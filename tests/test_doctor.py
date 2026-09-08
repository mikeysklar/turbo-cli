"""doctor's reporting, from fabricated facts (SPEC.md 5.1 and 6). The gathering
side is exercised against real hardware; this pins the wording and exit status."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import turbo_cli as t  # noqa: E402


def facts(**kw):
    f = {"mount": "/Volumes/CIRCUITPY", "mounts": 1, "port": "/dev/cu.usbmodem14201",
         "port_errors": [], "mpy": 0x2b06, "arch": "xtensawin", "abi": "6.3",
         "arch_source": "probe",
         "boot": {"version": "10.3.0", "board_id": "adafruit_metro_esp32s3",
                  "board_name": "Metro ESP32-S3", "machine": "ESP32S3", "uid": "AA"}}
    f.update(kw)
    return f


def test_ready_block(tmp_path):
    mc = tmp_path / "mpy-cross"
    mc.write_text("#!/bin/sh\necho 'CircuitPython 10.3.0 on 2026-08-31; "
                  "mpy-cross emitting mpy v6.3'\n")
    mc.chmod(0o755)
    lines, ready = t.doctor_lines(facts(), mpy_cross=str(mc), src=str(tmp_path / "none"))
    assert ready
    assert lines[0] == "board       Metro ESP32-S3                adafruit_metro_esp32s3"
    assert lines[1] == "port        /dev/cu.usbmodem14201"
    assert lines[2] == "drive       /Volumes/CIRCUITPY"
    assert lines[3] == "firmware    CircuitPython 10.3.0"
    assert lines[4] == "_mpy        0x2b06   arch xtensawin · mpy 6.3 · native loader present"
    assert lines[-1] == "ready       turbo build compiles -march=xtensawin"


def test_stock_firmware_is_arch_zero(tmp_path):
    lines, ready = t.doctor_lines(facts(mpy=0x0306, arch=None, abi="6.3"),
                                  src=str(tmp_path / "none"))
    assert not ready
    assert "_mpy        0x0306   arch 0, no native loader" in lines
    assert lines[-3].startswith("   This board runs stock CircuitPython 10.3.0.")
    assert lines[-1] == "   Flash turbo firmware for adafruit_metro_esp32s3 (see docs/build.md)."


def test_no_board_at_all(tmp_path):
    lines, ready = t.doctor_lines(
        facts(mount=None, mounts=0, port=None, mpy=None, arch=None, arch_source=None, boot={}),
        src=str(tmp_path / "none"))
    assert not ready
    assert lines[0] == "no board found"
    assert lines[1].startswith("   No CIRCUITPY drive and no serial port.")


def test_mount_only_guesses_the_arch_from_the_board_id(tmp_path):
    lines, _ = t.doctor_lines(facts(port=None, mpy=None, abi=None, arch_source="board_id"),
                              src=str(tmp_path / "none"))
    assert ("_mpy        no serial port found; arch from board id: xtensawin "
            "(loader presence unknown)") in lines


def test_dev_build_has_no_published_mpy_cross(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "platform_key", lambda *a: "macos-arm64")
    f = facts()
    f["boot"]["version"] = "10.3.0-5-ga80fa21afb-dirty"
    lines, ready = t.doctor_lines(f, src=str(tmp_path / "none"))
    assert not ready
    assert any(l.startswith("firmware 10.3.0-5-ga80fa21afb-dirty   no published mpy-cross")
               for l in lines)
    assert not any("s3.amazonaws.com" in l for l in lines)  # never a URL that 404s


def test_offline_prints_the_fetch_url_and_does_not_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("TURBO_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(t, "platform_key", lambda *a: "linux-amd64")
    monkeypatch.setattr(t, "fetch_mpy_cross", lambda *a, **k: pytest.fail("fetched"))
    lines, ready = t.doctor_lines(facts(), offline=True, src=str(tmp_path / "none"))
    assert not ready
    assert "toolchain   not cached  linux-amd64  10.3.0" in lines
    assert any(l.strip() == t.mpy_cross_url("10.3.0", "linux-amd64") for l in lines)
    assert lines[-1].startswith("not ready   ")


def test_intel_mac_has_no_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "platform_key", lambda *a: None)
    monkeypatch.setattr(t.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(t.platform, "machine", lambda: "x86_64")
    lines, ready = t.doctor_lines(facts(), src=str(tmp_path / "none"))
    assert not ready
    assert "host Darwin x86_64   Adafruit builds macOS arm64 only" in lines


@pytest.mark.parametrize("version, ok", [
    ("10.3.0", True), ("9.2.8", True), ("10.4.0-beta.1", False),
    ("10.3.0-5-ga80fa21afb-dirty", False), ("", False), (None, False)])
def test_is_release_version(version, ok):
    assert t.is_release_version(version) is ok


def test_project_line_counts_modules_and_stale(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pixels.py").write_text("x = 1\n")
    out = tmp_path / "lib" / "turbo"
    t.save_manifest(str(out), {"pixels": {"src": "src/pixels.py",
                                          "sha256": t.sha256("src/pixels.py"),
                                          "xtensawin": {"installed": "viper"}}})
    assert t.project_state(str(out), "xtensawin") == (1, 0)
    (tmp_path / "src" / "pixels.py").write_text("x = 2\n")
    assert t.project_state(str(out), "xtensawin") == (1, 1)
    assert t.project_state(str(out), "armv6m") is None
