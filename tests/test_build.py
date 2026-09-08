"""turbo build: the report lines, the hint table and the manifest rules (SPEC 5.3,
4.1 to 4.3, 6). compile_variant is stubbed, so no mpy-cross is needed."""
import argparse
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import turbo_cli as t  # noqa: E402

VIPER_ERR = ('Traceback (most recent call last):\n'
             '  File "/tmp/blend.py", line 5, in blend\n'
             "ViperTypeError: can't do binary op between 'int' and 'object'")


def stub(sizes):
    """A compile_variant that writes `sizes[tier]` bytes, or fails with sizes[tier]
    as the stderr when it is a string. Tier is read back out of the dest name."""
    def fake(mpy_cross, text, name, arch, dest):
        tier = dest.rsplit(".", 2)[-2]
        v = sizes[tier]
        if isinstance(v, str):
            return v
        with open(dest, "wb") as f:
            f.write(b"\x00" * v)
        return None
    return fake


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return "lib/turbo"


def run(monkeypatch, sizes, archs=("xtensawin",), out="lib/turbo"):
    monkeypatch.setattr(t, "compile_variant", stub(sizes))
    lines = []
    entry, installed, ok, failures = t.build_module(
        "mpy-cross", "pixels", "src/pixels.py", "@turbo\ndef f(): pass\n", list(archs),
        out, echo=lines.append)
    return lines, entry, installed, ok, failures


def test_both_tiers_compile(out, monkeypatch):
    lines, entry, installed, ok, failures = run(monkeypatch, {"viper": 639, "native": 1204})
    assert ok
    assert lines == ["pixels     viper   xtensawin    639 B      native   1,204 B"]
    assert entry["xtensawin"]["installed"] == "viper"
    assert entry["xtensawin"]["candidates"] == {"viper": 639, "native": 1204}
    assert entry["xtensawin"]["measured"] is False
    assert [a for a, _ in installed] == ["xtensawin"]
    assert os.path.getsize(installed[0][1]) == 639


def test_viper_fails_native_ships(out, monkeypatch):
    lines, entry, installed, ok, failures = run(monkeypatch, {"viper": VIPER_ERR, "native": 1204})
    assert ok  # the module still ships, just slower
    assert lines[0] == "pixels     FAILED  src/pixels.py:5"
    assert lines[1] == "           ViperTypeError: can't do binary op between 'int' and 'object'"
    assert lines[2] == "           a float reached a viper function; scale to integers"
    assert lines[3] == "           native  xtensawin  1,204 B   installed"
    assert entry["xtensawin"]["installed"] == "native"
    assert entry["xtensawin"]["candidates"]["viper"].startswith("failed: ViperTypeError")


def test_both_tiers_fail(out, monkeypatch):
    os.makedirs("lib/turbo/xtensawin")
    stale = "lib/turbo/xtensawin/pixels.mpy"
    open(stale, "wb").write(b"old")
    lines, entry, installed, ok, failures = run(monkeypatch, {"viper": VIPER_ERR, "native": VIPER_ERR})
    assert not ok
    assert installed == []
    assert entry["xtensawin"]["installed"] is None
    assert not os.path.exists(stale)  # no old binary standing in for new source
    assert not any("installed" in l for l in lines)


def test_the_module_name_prints_once_across_arches(out, monkeypatch):
    lines, entry, _, ok, _ = run(monkeypatch, {"viper": 604, "native": 900},
                              archs=("armv6m", "armv7emsp"))
    assert lines[0].startswith("pixels     viper   armv6m")
    assert lines[1].startswith("           viper   armv7emsp")
    assert set(entry) == {"armv6m", "armv7emsp"}


def test_copy_to_board_puts_everything_the_shim_needs_there(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "lib" / "turbo"
    (out / "armv6m").mkdir(parents=True)
    mpy = out / "armv6m" / "pixels.mpy"
    mpy.write_bytes(b"\x00" * 10)
    (tmp_path / "lib" / "turbo.py").write_text("# the project's shim\n")
    (tmp_path / "src").mkdir()
    src = tmp_path / "src" / "pixels.py"
    src.write_text("from turbo import turbo\n")
    t.save_manifest(str(out), {"pixels": {"src": "src/pixels.py"}})
    mount = tmp_path / "CIRCUITPY"
    mount.mkdir()

    assert t.copy_to_board(str(mount), str(out), [("armv6m", str(mpy))],
                           [str(src)]) == "1 module, shim, 1 source"
    assert (mount / "lib" / "turbo" / "armv6m" / "pixels.mpy").read_bytes() == b"\x00" * 10
    assert (mount / "lib" / "turbo.py").read_text() == "# the project's shim\n"
    assert (mount / "src" / "pixels.py").read_text() == "from turbo import turbo\n"
    assert json.loads((mount / "lib" / "turbo" / "turbo.json").read_text())["pixels"]
    # a second run writes nothing: every write costs the board an autoreload
    assert t.copy_to_board(str(mount), str(out), [("armv6m", str(mpy))], [str(src)]) == ""
    # a changed module alone is reported alone
    mpy.write_bytes(b"\x01" * 10)
    assert t.copy_to_board(str(mount), str(out), [("armv6m", str(mpy))],
                           [str(src)]) == "1 module"


def test_copy_to_board_never_touches_code_py(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "lib" / "turbo"
    (out / "armv6m").mkdir(parents=True)
    mpy = out / "armv6m" / "pixels.mpy"
    mpy.write_bytes(b"\x00")
    (tmp_path / "code.py").write_text("mine, on the host\n")
    mount = tmp_path / "CIRCUITPY"
    mount.mkdir()
    (mount / "code.py").write_text("theirs, on the board\n")
    t.copy_to_board(str(mount), str(out), [("armv6m", str(mpy))], [])
    assert (mount / "code.py").read_text() == "theirs, on the board\n"


def test_the_bundled_shim_is_used_when_the_project_has_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "lib" / "turbo"
    (out / "armv6m").mkdir(parents=True)
    mpy = out / "armv6m" / "pixels.mpy"
    mpy.write_bytes(b"\x00")
    mount = tmp_path / "CIRCUITPY"
    mount.mkdir()
    assert "shim" in t.copy_to_board(str(mount), str(out), [("armv6m", str(mpy))], [])
    assert (mount / "lib" / "turbo.py").read_text() == open(t.asset("shim", "turbo.py")).read()


def test_shim_source_prefers_the_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert t.shim_source("lib/turbo") == t.asset("shim", "turbo.py")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "turbo.py").write_text("# mine\n")
    assert t.shim_source("lib/turbo") == os.path.join("lib", "turbo.py")


@pytest.mark.parametrize("message, first_hint", [
    ("ViperTypeError: can't do binary op between 'int' and 'object'",
     "a float reached a viper function; scale to integers"),
    ("ViperTypeError: local 'x' has type 'int' but source is 'object'",
     "a value came from a Python object; declare the parameter type"),
    ("SyntaxError: invalid micropython decorator",
     "this firmware has no emitter; the decorator must go through turbo build,"),
    ("ValueError: incompatible .mpy arch",
     "(seen on the board) the .mpy is for a different arch; turbo doctor"),
    ("ValueError: native code in .mpy unsupported",
     "(seen on the board) stock firmware; see the arch 0 sentence above"),
])
def test_every_section_6_error_has_its_hint(message, first_hint):
    assert t.hint_for(message)[0] == first_hint


def test_an_unknown_error_gets_no_hint():
    assert t.hint_for("MemoryError: out of memory") == []


def test_compiler_error_reads_the_file_line():
    loc, message, hint = t.compiler_error(VIPER_ERR, "src/blend.py")
    assert loc == "src/blend.py:5"
    assert message == "ViperTypeError: can't do binary op between 'int' and 'object'"
    assert hint


def test_compiler_error_without_a_traceback():
    loc, message, hint = t.compiler_error("mpy-cross: unknown architecture", "src/x.py")
    assert loc == "src/x.py"
    assert message == "mpy-cross: unknown architecture"
    assert hint == []


def test_thousands():
    assert t.thousands(639) == "639"
    assert t.thousands(1204) == "1,204"


def test_build_on_stock_firmware_says_arch_0_not_no_board(tmp_path, monkeypatch, capsys):
    """A stock board is found; it just has no loader. Calling it missing sends the
    user looking for a cable instead of for firmware."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(t, "board_facts", lambda a: {
        "mount": "/media/sklarm/CIRCUITPY", "mounts": 1, "port": "/dev/ttyACM18",
        "port_errors": [], "mpy": 0x0306, "arch": None, "abi": "6.3",
        "arch_source": "probe",
        "boot": {"version": "10.3.0", "board_id": "adafruit_metro_rp2040",
                 "board_name": "Adafruit Metro RP2040"}})
    a = argparse.Namespace(src="src", out="lib/turbo", arch=None, mpy_cross=None,
                           offline=True, verbose=False, no_copy=True, port=None,
                           mount=None, board=None)
    assert t.cmd_build(a) == 1
    out = capsys.readouterr().out
    assert "_mpy        0x0306   arch 0, no native loader" in out
    assert "Flash turbo firmware for adafruit_metro_rp2040" in out
    assert "no board found" not in out


def test_a_drive_that_will_not_take_a_write_gets_a_sentence(tmp_path, monkeypatch, capsys):
    """Seen on the farm: the kernel remounted a CIRCUITPY drive read-only after an
    I/O error. The modules had compiled; only the copy failed."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pixels.py").write_text("from turbo import turbo\n\n\n@turbo\ndef f():\n    pass\n")
    mount = tmp_path / "CIRCUITPY"
    mount.mkdir()
    monkeypatch.setattr(t, "board_facts", lambda a: {
        "mount": str(mount), "mounts": 1, "port": None, "port_errors": [], "mpy": 0x1306,
        "arch": "armv6m", "abi": "6.3", "arch_source": "probe",
        "boot": {"version": "10.3.0", "board_id": "adafruit_metro_rp2040"}})
    monkeypatch.setattr(t, "compile_variant", stub({"viper": 603, "native": 635}))
    monkeypatch.setattr(t, "resolve_toolchain", lambda *a, **k: ("mpy-cross", []))
    monkeypatch.setattr(t, "copy_to_board", lambda *a, **k: (_ for _ in ()).throw(
        OSError(30, "Read-only file system")))
    a = argparse.Namespace(src="src", out="lib/turbo", arch=None, mpy_cross=None,
                           offline=True, verbose=False, no_copy=False, port=None,
                           mount=None, board=None)
    assert t.cmd_build(a) == 1
    out = capsys.readouterr().out
    assert "Read-only file system" in out
    assert "CircuitPython may have the filesystem for itself" in out
    assert out.rstrip().endswith("built but not copied")
    assert "Traceback" not in out
