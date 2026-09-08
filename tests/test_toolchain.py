"""Toolchain fetch, cache and validation (SPEC.md section 8). Serves the binary
from a local HTTP server: these tests never touch the network."""
import http.server
import os
import stat
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import turbo_cli as t  # noqa: E402

BODY = b"#!/bin/sh\necho 'CircuitPython 10.3.0 on 2026-08-31; mpy-cross emitting mpy v6.3'\n"


class Handler(http.server.BaseHTTPRequestHandler):
    body = BODY

    def do_GET(self):
        if self.path.endswith("-arm64"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(t, "MPY_CROSS_BASE", "http://127.0.0.1:%d/" % httpd.server_address[1])
    yield httpd
    httpd.shutdown()


def fake_binary(path, version="10.3.0", abi="6.3"):
    with open(path, "w") as f:
        f.write("#!/bin/sh\necho 'CircuitPython %s on 2026-08-31; "
                "mpy-cross emitting mpy v%s'\n" % (version, abi))
    os.chmod(path, 0o755)
    return path


def test_fetch_writes_an_executable_into_the_cache(server, tmp_path, monkeypatch):
    monkeypatch.setenv("TURBO_CACHE", str(tmp_path))
    path = t.fetch_mpy_cross("10.3.0", "macos-arm64")
    assert path == str(tmp_path / "mpy-cross" / "10.3.0" / "macos-arm64" / "mpy-cross")
    assert open(path, "rb").read() == BODY
    assert os.stat(path).st_mode & stat.S_IXUSR
    assert not os.path.exists(path + ".part")  # no half binary left behind
    assert t.cached_mpy_cross("10.3.0", "macos-arm64") == path
    assert t.cached_versions("macos-arm64") == ["10.3.0"]


def test_fetch_404_is_the_unpublished_version_sentence(server, tmp_path, monkeypatch):
    monkeypatch.setenv("TURBO_CACHE", str(tmp_path))
    with pytest.raises(t.ToolchainError) as e:
        t.fetch_mpy_cross("10.3.0", "linux-amd64")  # the handler only serves arm64
    assert e.value.lines[0] == "firmware 10.3.0   no published mpy-cross for that version"
    assert "per release only" in e.value.lines[1]
    assert t.cached_mpy_cross("10.3.0", "linux-amd64") is None


def test_fetch_unreachable_host_names_the_url(tmp_path, monkeypatch):
    monkeypatch.setenv("TURBO_CACHE", str(tmp_path))
    monkeypatch.setattr(t, "MPY_CROSS_BASE", "http://127.0.0.1:1/")
    with pytest.raises(t.ToolchainError) as e:
        t.fetch_mpy_cross("10.3.0", "macos-arm64")
    assert e.value.lines[0].startswith("mpy-cross download failed")
    assert "127.0.0.1:1" in e.value.lines[1]


def test_validate_accepts_a_matching_binary(tmp_path):
    p = fake_binary(str(tmp_path / "mpy-cross"))
    assert t.validate_mpy_cross(p, "10.3.0", "6.3") is None
    assert t.validate_mpy_cross(p, "10.3.0", None) is None  # no board, no abi to check
    assert os.path.exists(p)


def test_validate_deletes_an_abi_mismatch(tmp_path):
    p = fake_binary(str(tmp_path / "mpy-cross"), version="10.3.0", abi="6.2")
    bad = t.validate_mpy_cross(p, "10.3.0", "6.3")
    assert bad[0] == "mpy-cross 10.3.0 reports mpy v6.2, board wants v6.3; refusing to use it"
    assert not os.path.exists(p)


def test_validate_deletes_a_version_mismatch(tmp_path):
    p = fake_binary(str(tmp_path / "mpy-cross"), version="9.2.8")
    bad = t.validate_mpy_cross(p, "10.3.0", "6.3")
    assert "is 9.2.8, not 10.3.0" in bad[0]
    assert not os.path.exists(p)


def test_validate_deletes_a_binary_that_does_not_run(tmp_path):
    p = str(tmp_path / "mpy-cross")
    open(p, "wb").write(b"not an executable")
    bad = t.validate_mpy_cross(p, "10.3.0", "6.3")
    assert bad[0] == "mpy-cross 10.3.0 does not run on this host"
    assert not os.path.exists(p)


def test_doctor_fetches_then_reports_cached(server, tmp_path, monkeypatch):
    monkeypatch.setenv("TURBO_CACHE", str(tmp_path))
    monkeypatch.setattr(t, "platform_key", lambda *a: "macos-arm64")
    f = {"mount": "/Volumes/CIRCUITPY", "mounts": 1, "port": "/dev/cu.usbmodem1", "mpy": 0x2b06,
         "abi": "6.3", "arch": "xtensawin", "arch_source": "probe", "port_errors": [],
         "boot": {"version": "10.3.0", "board_id": "adafruit_metro_esp32s3",
                  "board_name": "Metro ESP32-S3"}}
    lines, ready = t.doctor_lines(f, src=str(tmp_path / "none"))
    assert ready
    assert "toolchain   fetching mpy-cross  macos-arm64  10.3.0" in lines
    assert any(l.strip().startswith("cached  ") for l in lines)
    # second run: one line, no fetch
    monkeypatch.setattr(t, "fetch_mpy_cross", lambda *a, **k: pytest.fail("fetched twice"))
    lines, ready = t.doctor_lines(f, src=str(tmp_path / "none"))
    assert ready
    assert len([l for l in lines if l.startswith("toolchain")]) == 1
    assert "mpy v6.3" in [l for l in lines if l.startswith("toolchain")][0]


def test_doctor_says_a_cached_other_version_is_the_wrong_format(server, tmp_path, monkeypatch):
    monkeypatch.setenv("TURBO_CACHE", str(tmp_path))
    monkeypatch.setattr(t, "platform_key", lambda *a: "macos-arm64")
    old = tmp_path / "mpy-cross" / "9.2.8" / "macos-arm64"
    old.mkdir(parents=True)
    fake_binary(str(old / "mpy-cross"), version="9.2.8", abi="6.2")
    f = {"mount": None, "mounts": 0, "port": "/dev/cu.usbmodem1", "mpy": 0x2b06, "abi": "6.3",
         "arch": "xtensawin", "arch_source": "probe", "port_errors": [],
         "boot": {"version": "10.3.0", "board_id": "adafruit_metro_esp32s3"}}
    lines, ready = t.doctor_lines(f, src=str(tmp_path / "none"))
    assert "mpy-cross 9.2.8 cached, board runs 10.3.0" in lines
    assert "   Different .mpy format. Fetching 10.3.0." in lines
    assert ready
