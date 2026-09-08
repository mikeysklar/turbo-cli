"""turbo build: the report lines, the hint table and the manifest rules (SPEC 5.3,
4.1 to 4.3, 6). compile_variant is stubbed, so no mpy-cross is needed."""
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


def test_copy_to_board(tmp_path):
    out = tmp_path / "lib" / "turbo"
    (out / "armv6m").mkdir(parents=True)
    mpy = out / "armv6m" / "pixels.mpy"
    mpy.write_bytes(b"\x00" * 10)
    t.save_manifest(str(out), {"pixels": {"src": "src/pixels.py"}})
    mount = tmp_path / "CIRCUITPY"
    mount.mkdir()
    assert t.copy_to_board(str(mount), str(out), [("armv6m", str(mpy))]) == 1
    assert (mount / "lib" / "turbo" / "armv6m" / "pixels.mpy").read_bytes() == b"\x00" * 10
    assert json.loads((mount / "lib" / "turbo" / "turbo.json").read_text())["pixels"]


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
