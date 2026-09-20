"""turbo build --target cpython: the report lines, the manifest and the failure
sentences. compile_cython is stubbed, so no Cython is needed."""
import argparse
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import turbo_cli as t  # noqa: E402

SO = "pixels.cpython-313-aarch64-linux-gnu.so"
VIPER = ("from turbo import turbo\n\n@turbo.viper\n"
         "def fill(out: ptr8, n: int):\n"
         "    for i in range(n):\n"
         "        out[i] = i\n")
SLOW = ("from turbo import turbo\n\n@turbo.viper\n"
        "def blend(out: ptr8, n: int):\n"
        "    ratio = 0.5\n"
        "    q = n / 2\n"
        "    for i in range(n):\n"
        "        out[i] = i\n")
CAST = ("from turbo import turbo\n\n@turbo.viper\n"
        "def poke(buf, n: int):\n"
        "    p = ptr8(buf)\n"
        "    p[0] = n\n")
CYTHON_OUTPUT = ("Error compiling Cython file:\n"
                 "------------------------------------------------------------\n"
                 "...\n"
                 "    out[i] = nope\n"
                 "             ^\n"
                 "------------------------------------------------------------\n\n"
                 "pixels.py:9:17: undeclared name not builtin: nope\n"
                 "Traceback (most recent call last):\n"
                 "Cython.Compiler.Errors.CompileError: pixels.py\n")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.mkdir("src")
    monkeypatch.setattr(t, "find_cythonize", lambda: "/usr/bin/cythonize")
    return tmp_path


def write(name, text):
    with open(os.path.join("src", name + ".py"), "w") as f:
        f.write(text)


def stub(monkeypatch, size=2048, fail=None):
    """A compile_cython that writes `size` bytes, or fails with `fail` as the output.
    Records the source it was handed."""
    seen = {}

    def fake(cythonize, text, name, dest_dir):
        seen[name] = text
        if fail:
            return None, fail
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, SO.replace("pixels", name))
        with open(dest, "wb") as f:
            f.write(b"\x00" * size)
        return dest, None
    monkeypatch.setattr(t, "compile_cython", fake)
    return seen


def build(capsys):
    a = argparse.Namespace(src="src", out="lib/turbo", target="cpython")
    rc = t.cmd_build(a)
    lines = capsys.readouterr().out.splitlines()
    with open("lib/turbo/turbo.json") as f:
        return rc, lines, json.load(f)


def test_a_viper_module_builds(project, monkeypatch, capsys):
    write("pixels", VIPER)
    seen = stub(monkeypatch)
    rc, lines, manifest = build(capsys)
    assert rc == 0
    assert lines[0] == "pixels     cython  cpython    2,048 B      1 typed"
    assert lines[1].startswith("1 built, ") and lines[1].endswith(" ms")
    assert "@cython.locals(i=cython.int)" in seen["pixels"]
    assert manifest["pixels"]["cpython"] == {
        "candidates": {"cython": 2048}, "installed": "cython", "measured": False,
        "file": SO, "typed": 1, "untyped": []}
    assert manifest["pixels"]["sha256"] == t.sha256("src/pixels.py")
    assert manifest["pixels"]["src"] == "src/pixels.py"


def test_the_board_is_never_asked(project, monkeypatch, capsys):
    write("pixels", VIPER)
    stub(monkeypatch)
    monkeypatch.setattr(t, "board_facts", lambda a: pytest.fail("read the board"))
    monkeypatch.setattr(t, "resolve_toolchain", lambda *a, **k: pytest.fail("mpy-cross"))
    assert build(capsys)[0] == 0


def test_untyped_locals_are_named(project, monkeypatch, capsys):
    write("blend", SLOW)
    stub(monkeypatch)
    rc, lines, manifest = build(capsys)
    assert rc == 0
    assert lines[0] == "blend      cython  cpython    2,048 B      1 typed"
    assert lines[1] == " " * 11 + "untyped: ratio, q"
    assert lines[2] == " " * 11 + "these stay Python objects; a loop that uses them stays slow"
    assert manifest["blend"]["cpython"]["untyped"] == ["ratio", "q"]


def test_a_module_without_turbo_is_skipped(project, monkeypatch, capsys):
    write("plain", "def f():\n    return 1\n")
    write("pixels", VIPER)
    stub(monkeypatch)
    rc, lines, manifest = build(capsys)
    assert rc == 0
    assert "plain      skipped no @turbo decorator" in lines
    assert lines[-1].startswith("1 built, 1 skipped, ")
    assert "plain" not in manifest


def test_a_ptr_cast_is_refused_and_the_rest_still_builds(project, monkeypatch, capsys):
    write("poke", CAST)
    write("pixels", VIPER)
    seen = stub(monkeypatch)
    rc, lines, manifest = build(capsys)
    assert rc == 1
    assert "poke" not in seen  # never handed to the compiler
    at = lines.index("poke       FAILED  src/poke.py")
    assert lines[at + 1] == (" " * 11 + "ptr8() cast: no Cython form yet, pass the buffer "
                             "as a typed argument")
    assert lines[-1].startswith("1 built, 1 failed, ")
    assert manifest["poke"]["cpython"]["installed"] is None
    assert "sha256" not in manifest["poke"]  # so `check` says STALE, not fresh
    assert manifest["pixels"]["cpython"]["installed"] == "cython"


def test_a_cython_error_prints_its_message_without_the_shifted_line(project, monkeypatch,
                                                                    capsys):
    write("pixels", VIPER)
    stub(monkeypatch, fail=CYTHON_OUTPUT)
    rc, lines, manifest = build(capsys)
    assert rc == 1
    assert lines[0] == "pixels     FAILED  src/pixels.py"
    assert lines[1] == " " * 11 + "undeclared name not builtin: nope"
    assert manifest["pixels"]["cpython"]["candidates"] == {
        "cython": "failed: undeclared name not builtin: nope"}


def test_a_c_compiler_error_falls_back_to_the_last_line():
    assert t.cython_error("gcc: warning\nerror: command 'gcc' failed\n") == \
        "error: command 'gcc' failed"
    assert t.cython_error("") == "cythonize failed"


def test_no_cython_is_a_sentence(project, monkeypatch, capsys):
    write("pixels", VIPER)
    monkeypatch.setattr(t, "find_cythonize", lambda: None)
    a = argparse.Namespace(src="src", out="lib/turbo", target="cpython")
    assert t.cmd_build(a) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Cython is not installed",
        "   --target cpython compiles with cythonize.   pip install cython"]
    assert not os.path.exists("lib")


def test_no_source_directory_is_a_sentence(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    a = argparse.Namespace(src="src", out="lib/turbo", target="cpython")
    assert t.cmd_build(a) == 1
    assert capsys.readouterr().out.splitlines()[0] == "no source directory src"


def test_check_reads_a_cpython_entry(project, monkeypatch, capsys):
    write("pixels", VIPER)
    stub(monkeypatch)
    build(capsys)
    assert t.cmd_check(argparse.Namespace(src="src", out="lib/turbo")) == 0
    assert capsys.readouterr().out.split() == ["pixels", "fresh", "cpython=cython?"]


@pytest.mark.parametrize("argv, target", [(["build"], "board"),
                                          (["build", "--target", "cpython"], "cpython")])
def test_the_target_flag_defaults_to_the_board(monkeypatch, argv, target):
    got = []
    monkeypatch.setattr(t, "cmd_build", lambda a: got.append(a.target) or 0)
    monkeypatch.setattr(sys, "argv", ["turbo"] + argv)
    with pytest.raises(SystemExit):
        t.main()
    assert got == [target]
