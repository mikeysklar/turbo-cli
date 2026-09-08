"""turbo init: the layout it creates, and that it never overwrites (SPEC.md 5.2)."""
import argparse
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import turbo_cli as t  # noqa: E402


def args(**kw):
    a = argparse.Namespace(port=None, mount=None, board=None, arch="xtensawin",
                           out="lib/turbo", src="src", example=False)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


@pytest.fixture
def project(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # --arch is enough; never touch a real board from a test
    monkeypatch.setattr(t, "board_facts", lambda a: {
        "arch": a.arch, "arch_source": "flag", "mount": None, "mounts": 0, "port": None,
        "port_errors": [], "mpy": None, "abi": None, "boot": {}})
    return tmp_path


def test_creates_the_layout(project, capsys):
    assert t.cmd_init(args()) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("wrote  lib/turbo.py              shim, ")
    assert out[0].endswith(" lines, identity decorators on stock firmware")
    assert out[1] == "%-7s%-26s%s" % ("made", "src/",
                                      "your source, kept off sys.path so it never shadows .mpy")
    assert out[2] == "%-7s%-26s%s" % ("made", "lib/turbo/xtensawin/",
                                      "where compiled modules land")
    assert (project / "lib" / "turbo.py").is_file()
    assert (project / "lib" / "turbo" / "xtensawin").is_dir()
    assert (project / "src").is_dir()


def test_the_shim_it_writes_is_the_bundled_one(project):
    t.cmd_init(args())
    assert (project / "lib" / "turbo.py").read_text() == open(t.asset("shim", "turbo.py")).read()


def test_second_run_keeps_everything(project, capsys):
    t.cmd_init(args())
    capsys.readouterr()
    assert t.cmd_init(args()) == 0
    assert all(l.startswith("kept   ") for l in capsys.readouterr().out.splitlines())


def test_never_overwrites_an_edited_shim(project):
    (project / "lib").mkdir()
    (project / "lib" / "turbo.py").write_text("# mine\n")
    t.cmd_init(args())
    assert (project / "lib" / "turbo.py").read_text() == "# mine\n"


def test_example_writes_pixels_and_code(project, capsys):
    assert t.cmd_init(args(example=True)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[3].startswith("wrote  src/pixels.py")
    assert out[4].startswith("wrote  code.py")
    assert "@turbo.viper" in (project / "src" / "pixels.py").read_text()
    assert "TURBO-SHIM-DONE" in (project / "code.py").read_text()


def test_example_leaves_an_existing_code_py_alone(project, capsys):
    (project / "code.py").write_text("print('mine')\n")
    t.cmd_init(args(example=True))
    out = capsys.readouterr().out.splitlines()
    assert out[4] == "%-7s%-26s%s" % ("kept", "code.py",
                                      "not overwritten; see examples/mandelbrot/code.py")
    assert (project / "code.py").read_text() == "print('mine')\n"


def test_no_arch_explains_instead_of_guessing(project, capsys):
    assert t.cmd_init(args(arch=None)) == 1
    assert capsys.readouterr().out.startswith("no board found")
    assert not (project / "lib").exists()


def test_assets_are_present_in_the_checkout():
    assert t.asset("shim", "turbo.py")
    assert t.asset("examples", "mandelbrot", "code.py")
    assert t.asset("examples", "mandelbrot", "src", "pixels.py")
    assert t.asset("nope") is None
