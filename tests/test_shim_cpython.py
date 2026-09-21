"""cli/turbo.py, the turbo module on CPython. It edits sys.path and builtins when it
is imported, so every case runs a small project in its own Python process. numba is
a fake on PYTHONPATH, so none is needed."""
import os
import subprocess
import sys

import pytest

CLI = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cli"))

PIXELS = ("from turbo import turbo\n\n"
          "@turbo.viper\n"
          "def fill(out: ptr8, n: int) -> int:\n"
          "    for i in range(n):\n"
          "        out[i] = i\n"
          "    return n\n")
CODE = ("import os\nimport turbo\nimport pixels\n"
        "buf = bytearray(4)\n"
        "print(os.path.relpath(pixels.__file__).replace(os.sep, '/'), pixels.fill(buf, 4), bytes(buf))\n")
FAKE_NUMBA = ("import types\n"
              "class NumbaError(Exception):\n    pass\n"
              "core = types.SimpleNamespace(errors=types.SimpleNamespace(NumbaError=NumbaError))\n"
              "def njit(cache=False):\n"
              "    def deco(f):\n"
              "        def fast(*a, **k):\n"
              "            if f.__name__ == 'hard':\n"
              "                raise NumbaError('Failed in nopython mode\\nmore')\n"
              "            print('jit', f.__name__, 'cache', cache)\n"
              "            return f(*a, **k)\n"
              "        return fast\n"
              "    return deco\n")


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pixels.py").write_text(PIXELS)
    (tmp_path / "code.py").write_text(CODE)
    return tmp_path


def run(project, script="code.py", cwd=None, extra_path=(), **env):
    full = {k: v for k, v in os.environ.items() if k != "TURBO"}
    full["PYTHONPATH"] = os.pathsep.join([CLI] + [str(p) for p in extra_path])
    full.update(env)
    r = subprocess.run([sys.executable, str(project / script)], cwd=str(cwd or project),
                       capture_output=True, text=True, env=full)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def built(project):
    """Stand in for a .so: a plain module in lib/turbo/cpython wins the same way."""
    d = project / "lib" / "turbo" / "cpython"
    d.mkdir(parents=True)
    (d / "pixels.py").write_text("def fill(out, n):\n    out[0] = 9\n    return -n\n")


def test_source_runs_with_the_viper_hints_defined(project):
    assert run(project) == ["src/pixels.py 4 b'\\x00\\x01\\x02\\x03'"]


def test_a_built_module_wins_over_source(project):
    built(project)
    assert run(project) == ["lib/turbo/cpython/pixels.py -4 b'\\t\\x00\\x00\\x00'"]


def test_turbo_source_ignores_built_files(project):
    built(project)
    assert run(project, TURBO="source") == ["src/pixels.py 4 b'\\x00\\x01\\x02\\x03'"]


def test_the_project_is_the_folder_of_the_script_not_the_cwd(project, tmp_path_factory):
    built(project)
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    out = run(project, cwd=elsewhere)
    assert out[0].endswith("lib/turbo/cpython/pixels.py -4 b'\\t\\x00\\x00\\x00'")


def test_an_unknown_mode_is_one_line_and_ignored(project):
    assert run(project, TURBO="bogus") == [
        "turbo: TURBO=bogus is not numba or source, ignored",
        "src/pixels.py 4 b'\\x00\\x01\\x02\\x03'"]


def test_numba_mode_jits_viper_with_the_cache_on(project, tmp_path_factory):
    fake = tmp_path_factory.mktemp("fake")
    (fake / "numba.py").write_text(FAKE_NUMBA)
    assert run(project, extra_path=[fake], TURBO="numba") == [
        "jit fill cache True", "src/pixels.py 4 b'\\x00\\x01\\x02\\x03'"]


def test_numba_mode_leaves_native_and_bare_turbo_alone(project, tmp_path_factory):
    fake = tmp_path_factory.mktemp("fake")
    (fake / "numba.py").write_text(FAKE_NUMBA)
    (project / "other.py").write_text(
        "from turbo import turbo\n"
        "@turbo\ndef a(x):\n    return x + 1\n"
        "@turbo.native\ndef b(x):\n    return x + 2\n"
        "print(a(1), b(1))\n")
    assert run(project, script="other.py", extra_path=[fake], TURBO="numba") == ["2 3"]


def test_what_numba_cannot_compile_runs_from_source_with_one_notice(project,
                                                                    tmp_path_factory):
    fake = tmp_path_factory.mktemp("fake")
    (fake / "numba.py").write_text(FAKE_NUMBA)
    (project / "hard.py").write_text(
        "from turbo import turbo\n"
        "@turbo.viper\ndef hard(x):\n    return x * 2\n"
        "print(hard(2), hard(3))\n")
    assert run(project, script="hard.py", extra_path=[fake], TURBO="numba") == [
        "turbo: numba cannot compile hard, running it from source",
        "   Failed in nopython mode",
        "4 6"]


def test_numba_mode_without_numba_runs_from_source(project):
    out = run(project, TURBO="numba", PYTHONNOUSERSITE="1")
    if out[0].startswith("turbo: TURBO=numba but numba is not installed"):
        assert out == ["turbo: TURBO=numba but numba is not installed, fill runs from source",
                       "src/pixels.py 4 b'\\x00\\x01\\x02\\x03'"]
    else:  # a real numba is installed here
        assert out[-1] == "src/pixels.py 4 b'\\x00\\x01\\x02\\x03'"


def test_the_casts_and_what_the_module_reports(project):
    (project / "casts.py").write_text(
        "import turbo\n"
        "b = bytearray(2)\n"
        "print(ptr8(b) is b, ptr16(b) is b, ptr32(b) is b, ptr(b) is b, uint(-1))\n"
        "print(turbo.arch, turbo.mode == '', [p.replace('\\\\', '/').split('/')[-1] "
        "for p in turbo.paths], turbo.path == turbo.paths[0])\n")
    assert run(project, script="casts.py") == ["True True True True 4294967295",
                                               "cpython True ['src'] True"]


def test_no_project_folders_is_not_an_error(tmp_path):
    (tmp_path / "solo.py").write_text("import turbo\nprint(turbo.paths, turbo.path)\n")
    assert run(tmp_path, script="solo.py") == ["[] None"]
