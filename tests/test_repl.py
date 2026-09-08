"""Raw REPL framing and port selection, against a scripted fake serial port.
No hardware: these check that we speak the protocol in SPEC.md 7.2 and 7.3."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import turbo_repl as r  # noqa: E402


class FakeSerial:
    """Queues each response once the bytes that trigger it have been written."""

    def __init__(self, script=(), **kw):
        self.script = list(script)
        self.written = bytearray()
        self.buf = bytearray()
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.buf)

    def write(self, data):
        self.written.extend(data)
        while self.script and self.script[0][0] in bytes(self.written):
            self.buf.extend(self.script.pop(0)[1])
        return len(data)

    def read(self, n=1):
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        return chunk

    def reset_input_buffer(self):
        self.buf.clear()

    def close(self):
        self.closed = True


def make(script):
    """A RawREPL wired to a FakeSerial running `script`."""
    fake = FakeSerial(script)
    repl = r.RawREPL.__new__(r.RawREPL)
    repl.port, repl.ser, repl._pending, repl._raw = "/dev/fake", fake, b"", False
    return repl, fake


RAW = [(b"\r\x01", r.RAW_BANNER)]


def test_enter_raw_interrupts_then_switches_mode():
    repl, fake = make(RAW)
    repl.enter_raw()
    assert bytes(fake.written) == b"\r\x03\x03\r\x01"
    assert repl._raw


def test_exec_splits_stdout_and_stderr():
    code = "print(1)"
    reply = b"OK" + b"hello\r\n" + b"\x04" + b"" + b"\x04" + b">"
    repl, fake = make(RAW + [(code.encode() + b"\x04", reply)])
    out, err = repl.exec(code)
    assert (out, err) == ("hello\r\n", "")


def test_exec_returns_the_traceback_as_stderr():
    code = "import sys; print(sys.implementation._mpy)"
    tb = b"Traceback (most recent call last):\r\n  File \"<stdin>\", line 1\r\n" \
         b"AttributeError: 'implementation' object has no attribute '_mpy'\r\n"
    repl, fake = make(RAW + [(code.encode() + b"\x04", b"OK" + b"\x04" + tb + b"\x04>")])
    out, err = repl.exec(code)
    assert out == ""
    assert "AttributeError" in err


def test_exec_survives_a_reply_that_arrives_in_one_chunk_past_a_marker():
    # one read can span several framing markers; the leftovers must carry over
    code = "x"
    repl, fake = make(RAW + [(code.encode() + b"\x04", b"OK7942 (10, 3, 0)\r\n\x04\x04>")])
    assert repl.exec(code) == ("7942 (10, 3, 0)\r\n", "")


def test_timeout_names_the_port_and_what_was_missing():
    repl, fake = make([])  # board says nothing
    with pytest.raises(r.REPLError) as e:
        repl.enter_raw(timeout=0.05)
    assert "/dev/fake" in str(e.value) and "raw REPL banner" in str(e.value)


def test_close_leaves_raw_mode_and_never_resets():
    repl, fake = make(RAW)
    repl.enter_raw()
    repl.close()
    assert bytes(fake.written).endswith(b"\r\x02")
    assert b"\x04" not in bytes(fake.written)  # no soft reset
    assert fake.closed


@pytest.mark.parametrize("out, err, want", [
    ("7942 (10, 3, 0)\r\n", "", (7942, "10.3.0")),
    ("774 (10, 3, 0)", "", (774, "10.3.0")),
    ("11014 (10, 3, 0)", "", (11014, "10.3.0")),
    ("", "AttributeError: 'implementation' object has no attribute '_mpy'", (0, None)),
])
def test_parse_probe(out, err, want):
    assert r.parse_probe(out, err) == want


def test_parse_probe_rejects_junk():
    with pytest.raises(r.REPLError):
        r.parse_probe("no idea\r\n", "")
    with pytest.raises(r.REPLError):
        r.parse_probe("", "MemoryError: memory allocation failed")


def _ports(monkeypatch, entries, system="Linux"):
    mk = lambda d, v, desc, sn: types.SimpleNamespace(  # noqa: E731
        device=d, vid=v, description=desc, serial_number=sn)
    fake = types.SimpleNamespace(tools=types.SimpleNamespace(
        list_ports=types.SimpleNamespace(comports=lambda: [mk(*e) for e in entries])))
    monkeypatch.setattr(r, "serial", fake)
    monkeypatch.setattr(r.platform, "system", lambda: system)


def test_find_ports_keeps_adafruit_and_circuitpython_only(monkeypatch):
    _ports(monkeypatch, [("/dev/ttyACM0", 0x239A, "Metro ESP32-S3", "AA"),
                         ("/dev/ttyUSB0", 0x10C4, "CP2102 UART Bridge", None),
                         ("/dev/ttyACM1", 0x2E8A, "Board in FS mode CircuitPython", "BB")])
    assert r.find_ports() == ["/dev/ttyACM0", "/dev/ttyACM1"]


def test_find_ports_prefers_cu_on_macos(monkeypatch):
    _ports(monkeypatch, [("/dev/tty.usbmodem14201", 0x239A, "Metro", "AA"),
                         ("/dev/cu.usbmodem14201", 0x239A, "Metro", "AA")], system="Darwin")
    assert r.find_ports() == ["/dev/cu.usbmodem14201"]


def test_find_ports_lists_the_repl_before_the_data_port(monkeypatch):
    _ports(monkeypatch, [("/dev/cu.usbmodem14203", 0x239A, "Metro", "AA"),
                         ("/dev/cu.usbmodem14201", 0x239A, "Metro", "AA")], system="Darwin")
    assert r.find_ports()[0] == "/dev/cu.usbmodem14201"


def test_find_ports_uid_wins_over_name_order(monkeypatch):
    _ports(monkeypatch, [("/dev/ttyACM0", 0x239A, "Metro", "0123456789ABCDEF"),
                         ("/dev/ttyACM1", 0x239A, "Feather", "FEDCBA9876543210")])
    assert r.find_ports(uid="fedcba9876543210")[0] == "/dev/ttyACM1"


def test_find_ports_explicit_is_the_only_candidate(monkeypatch):
    _ports(monkeypatch, [("/dev/ttyACM0", 0x239A, "Metro", "AA")])
    assert r.find_ports(explicit="/dev/whatever") == ["/dev/whatever"]


def test_soft_reset_waits_for_both_banners():
    repl, fake = make(RAW + [(b"\x04", b"soft reboot\r\n" + r.RAW_BANNER)])
    repl.enter_raw()
    repl.soft_reset()
    assert bytes(fake.written) == b"\r\x03\x03\r\x01\x04"
    assert repl._raw


def test_soft_reset_times_out_if_the_board_does_not_come_back():
    repl, fake = make(RAW)
    repl.enter_raw()
    with pytest.raises(r.REPLError) as e:
        repl.soft_reset(timeout=0.05)
    assert "soft reboot banner" in str(e.value)


def test_resume_leaves_raw_mode_then_reloads():
    repl, fake = make(RAW)
    repl.enter_raw()
    repl.resume()
    # Ctrl-B out of raw mode, then Ctrl-D so CircuitPython runs code.py again
    assert bytes(fake.written).endswith(b"\r\x02\r\x04")
    assert not repl._raw
