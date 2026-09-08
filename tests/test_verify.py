"""turbo verify: running two versions on host CPython and reporting the gap
(SPEC.md 5.5). No board, no mpy-cross."""
import argparse
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import turbo_cli as t  # noqa: E402

EX = os.path.join(os.path.dirname(__file__), "..", "cli", "turbo_assets",
                  "examples", "mandelbrot")

VIPER_SRC = '''from turbo import turbo


@turbo.viper
def scale(out: ptr8, n: int, k: int):
    for i in range(n):
        out[i] = (int(out[i]) * k) >> 8


def _turbo_bench():
    b = bytearray(range(16))
    scale(b, 16, 128)
    return sum(b)
'''


def args(**kw):
    a = argparse.Namespace(baseline=None, candidate=None, fn=None, inputs=None,
                           verbose=False)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_load_on_host_runs_a_viper_module(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(VIPER_SRC)
    ns = t.load_on_host(str(p))
    assert callable(ns["scale"])
    assert ns["_turbo_bench"]() == 56


def test_the_stubs_do_not_leak(tmp_path):
    import builtins
    p = tmp_path / "m.py"
    p.write_text(VIPER_SRC)
    t.load_on_host(str(p))
    assert not hasattr(builtins, "ptr8")
    assert "turbo" not in sys.modules


def test_load_on_host_reports_a_broken_file(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("def f(:\n")
    with pytest.raises(t.VerifyError) as e:
        t.load_on_host(str(p))
    assert "did not run on the host" in str(e.value)


def test_missing_file():
    with pytest.raises(t.VerifyError):
        t.load_on_host("/nope/nothing.py")


@pytest.mark.parametrize("a, b, same, summary", [
    (5, 5, True, "identical"),
    (100, 90, False, "delta -10 (10.00%)"),
    ([1, 2, 3], [1, 2, 3], True, "identical, 3 values"),
    ([1, 2, 3], [1, 9, 3], False, "1 of 3 differ, max 7, mean 2.33"),
    ("x", "x", True, "identical"),
    ("x", "y", False, "differ"),
])
def test_compare_values(a, b, same, summary):
    got_same, _, _, got_summary = t.compare_values(a, b)
    assert (got_same, got_summary) == (same, summary)


def test_compare_buffers_shows_the_sums():
    _, sa, sb, _ = t.compare_values(bytearray([1, 2]), bytearray([1, 5]))
    assert (sa, sb) == ("3", "6")


def test_the_bundled_example_is_the_documented_number(capsys):
    rc = t.cmd_verify(args(baseline=os.path.join(EX, "src", "mandel_float.py"),
                           candidate=os.path.join(EX, "src", "pixels.py"),
                           fn="mandel_row"))
    out = capsys.readouterr().out
    assert rc == 0  # a difference is information, not failure
    assert "  checksum   407790 -> 407644" in out
    assert out.rstrip().endswith(
        "fixed point moved the answer. this is the number a reviewer wants to see.")


def test_the_cases_harness_compares_the_written_buffer(capsys):
    rc = t.cmd_verify(args(baseline=os.path.join(EX, "src", "mandel_float.py"),
                           candidate=os.path.join(EX, "src", "pixels.py"),
                           fn="mandel_row", inputs=os.path.join(EX, "cases.py")))
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out[0] == "120 rows x 160 px, 64 iterations, both versions run on the host"
    # the mutated argument is labelled by its parameter name, not its position
    assert out[1].startswith("  out        407790 -> 407644        335 of 19,200 differ")


def test_a_module_compared_with_itself_is_identical(capsys):
    p = os.path.join(EX, "src", "pixels.py")
    rc = t.cmd_verify(args(baseline=p, candidate=p, fn="mandel_row",
                           inputs=os.path.join(EX, "cases.py")))
    out = capsys.readouterr().out
    assert rc == 0
    assert "identical, 19,200 values" in out
    assert out.rstrip().endswith("identical output")


def test_no_inputs_and_no_bench_is_a_harness_error(tmp_path, capsys):
    p = tmp_path / "m.py"
    p.write_text("def f(x):\n    return x\n")
    rc = t.cmd_verify(args(baseline=str(p), candidate=str(p), fn="f"))
    assert rc == 2
    assert capsys.readouterr().out.startswith("no inputs: pass --inputs")


def test_a_missing_function_is_a_harness_error(capsys):
    rc = t.cmd_verify(args(baseline=os.path.join(EX, "src", "mandel_float.py"),
                           candidate=os.path.join(EX, "src", "pixels.py"),
                           fn="nope", inputs=os.path.join(EX, "cases.py")))
    assert rc == 2
    assert "has no function nope()" in capsys.readouterr().out


def test_verbose_prints_the_width_caveat(capsys):
    t.cmd_verify(args(baseline=os.path.join(EX, "src", "mandel_float.py"),
                      candidate=os.path.join(EX, "src", "pixels.py"),
                      fn="mandel_row", verbose=True))
    out = capsys.readouterr().out
    assert "viper ints wrap at 32 bits" in out
    assert "--fn is unused without --inputs" in out
