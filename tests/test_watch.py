"""turbo watch: the per-change line, the mtime poll and the reload banner
(SPEC.md 5.6). The loop itself is driven end to end against a stand-in drive."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import turbo_cli as t  # noqa: E402

FAIL = [("armv6m", "viper", "src/blend.py:5",
         "ViperTypeError: can't do binary op between 'int' and 'object'")]


def test_a_clean_rebuild():
    assert t.watch_report("pixels.py", [], 2, True, False) == (
        t.time.strftime("%H:%M:%S") + "  pixels.py changed   rebuilt 2 variants   copied")


def test_a_reload_seen_on_the_port():
    assert t.watch_report("pixels.py", [], 2, True, True).endswith(
        "rebuilt 2 variants   copied   board reloaded")


def test_one_variant_is_singular():
    assert "rebuilt 1 variant   " in t.watch_report("pixels.py", [], 1, True, False)


def test_a_viper_failure_that_still_ships():
    line = t.watch_report("blend.py", FAIL, 1, True, False, tier="native")
    assert "blend.py changed    FAILED src/blend.py:5" in line
    assert line.endswith("copied as native")
    assert "ViperTypeError: can't do binary op between 'int..." in line  # truncated


def test_a_total_failure_copies_nothing():
    assert t.watch_report("blend.py", FAIL, 0, False, False).endswith("not copied")


def test_the_two_name_widths_line_up():
    a = t.watch_report("pixels.py", [], 1, True, False)
    b = t.watch_report("blend.py", [], 1, True, False)
    assert a.index("rebuilt") == b.index("rebuilt")


def test_source_mtimes_reads_only_python(tmp_path):
    (tmp_path / "a.py").write_text("x\n")
    (tmp_path / "b.txt").write_text("x\n")
    (tmp_path / "sub").mkdir()
    assert set(t.source_mtimes(str(tmp_path))) == {"a.py"}


def test_source_mtimes_survives_a_file_vanishing(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x\n")
    monkeypatch.setattr(t.os.path, "getmtime", lambda p: (_ for _ in ()).throw(OSError()))
    assert t.source_mtimes(str(tmp_path)) == {}


class FakeSerial:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, n=1):
        return self.chunks.pop(0) if self.chunks else b""


def test_saw_reload_finds_the_banner():
    assert t.saw_reload(FakeSerial([b"code.py output\r\n", b"soft reboot\r\n"]))


def test_saw_reload_is_case_insensitive():
    assert t.saw_reload(FakeSerial([b"Soft reboot\r\n"]))


def test_saw_reload_times_out_quietly():
    assert t.saw_reload(FakeSerial([]), timeout=0.05) is False


def test_saw_reload_without_a_port():
    assert t.saw_reload(None) is False
