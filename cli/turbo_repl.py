"""Raw REPL over USB serial: find the board's port, run one exec, read stdout and
stderr back. pyserial only, so no CircuitPython checkout is needed (SPEC.md 7.2,
7.3). Replaces tools/pyboard.py for what the CLI does.

Nothing here soft-resets the board. The interrupt in enter_raw stops a running
code.py; CircuitPython resumes it on the next reload or Ctrl-D.
"""
import platform
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # the sentence, not a traceback
    serial = None

ADAFRUIT_VID = 0x239A
BAUD = 115200
RAW_BANNER = b"raw REPL; CTRL-B to exit\r\n>"
PROBE = "import sys; print(sys.implementation._mpy, sys.implementation.version)"

NO_PYSERIAL = ("pyserial is not installed\n"
               "   The serial probe needs it.   pip install pyserial\n"
               "   Or work without a board:   turbo build --arch NAME")


class REPLError(Exception):
    """The board did not answer as a raw REPL within the timeout."""


def find_ports(explicit=None, uid=None):
    """Candidate REPL ports, best first (SPEC 7.2). Adafruit VID or a description
    naming CircuitPython; on macOS the /dev/cu.* form, which does not block on DCD.
    A board with two CDC ports lists the REPL first and the data port second, so
    the caller tries them in order. `uid` (from boot_out.txt) matches the USB serial
    number when the port reports one, which pins the right board when several are
    plugged in."""
    if explicit:
        return [explicit]
    if serial is None:
        raise REPLError(NO_PYSERIAL)
    ports = [p for p in serial.tools.list_ports.comports()
             if p.vid == ADAFRUIT_VID or "circuitpython" in (p.description or "").lower()]
    if platform.system() == "Darwin":
        ports = [p for p in ports if "/cu." in p.device] or ports
    ports.sort(key=lambda p: (0 if uid and (p.serial_number or "").upper() == uid.upper() else 1,
                              p.device))
    return [p.device for p in ports]


class RawREPL:
    """One serial connection in raw mode. Holds the port and the read leftovers."""

    def __init__(self, port, baud=BAUD, timeout=2.0):
        if serial is None:
            raise REPLError(NO_PYSERIAL)
        self.port = port
        self.ser = serial.Serial(port, baud, timeout=timeout, write_timeout=timeout)
        self._pending = b""
        self._raw = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            if self._raw:
                self.exit_raw()
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass

    def _read_until(self, want, deadline, what):
        """Bytes received before `want`, consuming it. Anything read past it is kept
        for the next call, because one serial read can span two framing markers."""
        buf = bytearray(self._pending)
        self._pending = b""
        while True:
            i = buf.find(want)
            if i >= 0:
                self._pending = bytes(buf[i + len(want):])
                return bytes(buf[:i])
            if time.monotonic() >= deadline:
                tail = bytes(buf[-120:]).decode(errors="replace")
                raise REPLError("%s: no %s within the timeout; last saw %r"
                                % (self.port, what, tail))
            buf.extend(self.ser.read(getattr(self.ser, "in_waiting", 0) or 1))

    def enter_raw(self, timeout=3.0):
        """Interrupt whatever is running and enter raw mode. The \\r before Ctrl-A is
        the keypress CircuitPython waits for after code.py stops."""
        deadline = time.monotonic() + timeout
        self.ser.write(b"\r\x03\x03")
        time.sleep(0.1)
        try:
            self.ser.reset_input_buffer()  # drop the KeyboardInterrupt traceback
        except Exception:
            pass
        self._pending = b""
        self.ser.write(b"\r\x01")
        self._read_until(RAW_BANNER, deadline, "raw REPL banner")
        self._raw = True

    def exec(self, code, timeout=10.0):
        """Run `code` on the board. Returns (stdout, stderr) as text; stderr holds the
        traceback when the code raised. Framing: OK, stdout, \\x04, stderr, \\x04, >."""
        if not self._raw:
            self.enter_raw()
        deadline = time.monotonic() + timeout
        self.ser.write(code.encode() + b"\x04")
        self._read_until(b"OK", deadline, "OK for the exec")
        out = self._read_until(b"\x04", deadline, "end of stdout")
        err = self._read_until(b"\x04", deadline, "end of stderr")
        self._read_until(b">", deadline, "raw prompt")
        return out.decode(errors="replace"), err.decode(errors="replace")

    def soft_reset(self, timeout=10.0):
        """Ctrl-D at the raw prompt: restart the VM so the next import re-reads the
        drive instead of returning the cached module. The only place turbo resets a
        board, and bench is the only caller. Byte sequence mirrors pyboard.py."""
        if not self._raw:
            self.enter_raw()
        deadline = time.monotonic() + timeout
        self._pending = b""
        self.ser.write(b"\x04")
        self._read_until(b"soft reboot\r\n", deadline, "soft reboot banner")
        self._read_until(RAW_BANNER, deadline, "raw REPL banner after the soft reset")

    def exit_raw(self):
        self.ser.write(b"\r\x02")
        self._raw = False


def parse_probe(out, err):
    """(mpy, version) from the PROBE line. mpy is 0 when the firmware has no
    sys.implementation._mpy, which is the same story as arch 0: no native loader."""
    if "AttributeError" in err:
        return 0, None
    if err.strip():
        raise REPLError("probe raised on the board: %s" % err.strip().splitlines()[-1])
    first, _, rest = out.strip().partition(" ")
    try:
        mpy = int(first)
    except ValueError:
        raise REPLError("probe printed %r, not an _mpy value" % out.strip()[:80])
    nums = [w for w in rest.strip("()").replace(",", " ").split() if w.isdigit()]
    return mpy, ".".join(nums) if nums else None


def probe(port, timeout=3.0):
    """(mpy, version) from one board. Raises REPLError if the port is not a REPL."""
    r = RawREPL(port, timeout=min(timeout, 2.0))
    try:
        r.enter_raw(timeout=timeout)
        return parse_probe(*r.exec(PROBE, timeout=timeout))
    finally:
        r.close()


def probe_any(ports, timeout=3.0):
    """Try each candidate and keep the first that answers as a raw REPL. Returns
    (port, mpy, version, errors); (None, None, None, errors) when none answered."""
    errors = []
    for p in ports:
        try:
            mpy, version = probe(p, timeout=timeout)
            return p, mpy, version, errors
        except (REPLError, OSError) as e:
            errors.append((p, str(e)))
    return None, None, None, errors
