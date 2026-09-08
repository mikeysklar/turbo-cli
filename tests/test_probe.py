"""Unit tests for the board-fact layer (SPEC.md section 10, no hardware)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import turbo_cli as t  # noqa: E402

SAMPLE = ("Adafruit CircuitPython 10.3.0 on 2026-08-31; Metro ESP32-S3 with ESP32S3\n"
          "Board ID:adafruit_metro_esp32s3\n"
          "UID:0123456789ABCDEF\n")


@pytest.mark.parametrize("mpy, arch", [
    (0x0306, None), (0x1306, "armv6m"), (0x1f06, "armv7emsp"),
    (0x2b06, "xtensawin"), (0x2f06, "rv32imc")])
def test_decode_mpy(mpy, arch):
    d = t.decode_mpy(mpy)
    assert (d["arch"], d["abi"]) == (arch, "6.3")
    assert d["arch_id"] == (t.ARCH_ID[arch] if arch else 0)


def test_decode_mpy_matches_shim_table():
    # 2.2: the CLI table and shim/turbo.py _ARCH must agree
    src = open(os.path.join(os.path.dirname(__file__), "..", "cli", "turbo_assets", "shim",
                          "turbo.py")).read()
    ns = {}
    exec(src.split("arch = ")[0], ns)  # imports plus the _ARCH table only
    assert ns["_ARCH"] == t.ARCH_NAME


def test_parse_boot_out_sample():
    f = t.parse_boot_out(SAMPLE)
    assert f["version"] == "10.3.0"
    assert f["build_date"] == "2026-08-31"
    assert f["board_name"] == "Metro ESP32-S3"
    assert f["machine"] == "ESP32S3"
    assert f["board_id"] == "adafruit_metro_esp32s3"
    assert f["uid"] == "0123456789ABCDEF"


def test_parse_boot_out_with_bootpy_output():
    f = t.parse_boot_out(SAMPLE + "hello from boot.py\nBoard ID:not_this_one\n")
    assert f["board_id"] == "adafruit_metro_esp32s3"
    assert f["version"] == "10.3.0"


def test_parse_boot_out_missing_board_id():
    f = t.parse_boot_out(
        "Adafruit CircuitPython 9.2.8 on 2025-05-28; Adafruit Metro RP2350 with rp2350b\n")
    assert f["version"] == "9.2.8"
    assert f["board_name"] == "Adafruit Metro RP2350"
    assert f["board_id"] is None and f["uid"] is None


def test_parse_boot_out_garbage():
    f = t.parse_boot_out("")
    assert all(v is None for v in f.values())


@pytest.mark.parametrize("system, machine, key, path", [
    ("Darwin", "arm64", "macos-arm64", "macos/mpy-cross-macos-10.3.0-arm64"),
    ("Linux", "x86_64", "linux-amd64", "linux-amd64/mpy-cross-linux-amd64-10.3.0.static"),
    ("Linux", "aarch64", "linux-aarch64",
     "linux-aarch64/mpy-cross-linux-aarch64-10.3.0.static-aarch64"),
    ("Linux", "armv7l", "linux-raspbian",
     "linux-raspbian/mpy-cross-linux-raspbian-10.3.0.static-raspbian"),
    ("Windows", "AMD64", "windows", "windows/mpy-cross-windows-10.3.0.static.exe")])
def test_mpy_cross_url(system, machine, key, path):
    assert t.platform_key(system, machine) == key
    assert t.mpy_cross_url("10.3.0", key) == t.MPY_CROSS_BASE + path


def test_platform_key_unpublished():
    assert t.platform_key("Darwin", "x86_64") is None
    assert t.platform_key("Linux", "i686") is None


def test_find_mounts(tmp_path, monkeypatch):
    a = tmp_path / "A"
    b = tmp_path / "B"
    empty = tmp_path / "C"
    for d in (a, b, empty):
        d.mkdir()
    (a / "boot_out.txt").write_text(SAMPLE)
    (b / "boot_out.txt").write_text(
        SAMPLE.replace("adafruit_metro_esp32s3", "adafruit_metro_rp2350"))
    monkeypatch.setenv("CIRCUITPY_MOUNT", str(a))
    # explicit --mount wins and is the only candidate
    found = t.find_mounts(explicit=str(b))
    assert [p for p, _ in found] == [str(b)]
    # a directory without boot_out.txt does not count
    assert t.find_mounts(explicit=str(empty)) == []
    # env var candidate
    found = t.find_mounts(system="Plan9")
    assert [p for p, _ in found] == [str(a)]
    # --board reorders
    monkeypatch.setattr(t, "_mount_candidates", lambda system: iter([str(a), str(b)]))
    found = t.find_mounts(board="adafruit_metro_rp2350")
    assert [f["board_id"] for _, f in found] == ["adafruit_metro_rp2350", "adafruit_metro_esp32s3"]


def test_linux_sees_numbered_drives(monkeypatch):
    """A farm host mounts eight boards as CIRCUITPY, CIRCUITPY1 ... CIRCUITPY7."""
    seen = []

    def fake_glob(pattern):
        seen.append(pattern)
        if pattern == "/media/*/CIRCUITPY*":
            return ["/media/sklarm/CIRCUITPY", "/media/sklarm/CIRCUITPY1",
                    "/media/sklarm/CIRCUITPY7"]
        return []

    monkeypatch.delenv("CIRCUITPY_MOUNT", raising=False)
    monkeypatch.setattr(t.glob, "glob", fake_glob)
    assert list(t._mount_candidates("Linux")) == [
        "/media/sklarm/CIRCUITPY", "/media/sklarm/CIRCUITPY1", "/media/sklarm/CIRCUITPY7"]
    assert "/media/*/CIRCUITPY*" in seen


def test_macos_sees_numbered_drives(monkeypatch):
    monkeypatch.delenv("CIRCUITPY_MOUNT", raising=False)
    monkeypatch.setattr(t.glob, "glob",
                        lambda p: ["/Volumes/CIRCUITPY", "/Volumes/CIRCUITPY 1"])
    assert list(t._mount_candidates("Darwin")) == ["/Volumes/CIRCUITPY",
                                                   "/Volumes/CIRCUITPY 1"]
