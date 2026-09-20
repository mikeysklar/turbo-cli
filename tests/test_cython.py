"""rewrite_cython: the cpython target's source rewrite. Text in, text out, so no
Cython and no Raspberry Pi is needed."""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))
import turbo_cli as t  # noqa: E402

MANDEL = os.path.join(os.path.dirname(__file__), "..", "cli", "turbo_assets", "examples",
                      "mandelbrot", "src", "pixels.py")

VIPER = '''from turbo import turbo


@turbo.viper
def total(buf: ptr16, n: int) -> int:
    t = 0
    for k in range(n):
        t += buf[k] * k
    return t
'''


def rewrite(src):
    out, report = t.rewrite_cython(src)
    ast.parse(out)  # whatever comes back is still Python
    return out, report


def test_a_module_without_turbo_is_left_alone():
    assert t.rewrite_cython("def f():\n    return 1\n") == (None, {})


def test_viper_gets_the_flags_the_types_and_the_locals():
    out, report = rewrite(VIPER)
    assert out == '''import cython


@cython.boundscheck(False)
@cython.wraparound(False)
@cython.cdivision(True)
@cython.locals(t=cython.int, k=cython.int)
def total(buf: cython.ushort[:], n: cython.int) -> int:
    t = 0
    for k in range(n):
        t += buf[k] * k
    return t
'''
    assert report == {"total": {"typed": ["t", "k"], "untyped": [], "casts": []}}


def test_the_bundled_mandelbrot_types_every_local_and_keeps_its_body():
    with open(MANDEL) as f:
        src = f.read()
    out, report = rewrite(src)
    assert report["mandel_row"] == {"typed": ["px", "cx", "x", "y", "i", "x2", "y2"],
                                    "untyped": [], "casts": []}
    body = "    # fixed point"
    assert out[out.index(body):] == src[src.index(body):]
    assert "out: cython.uchar[:]" in out and "max_iter: cython.int" in out


def test_every_hint_maps():
    out, _ = rewrite("from turbo import turbo\n@turbo.viper\n"
                     "def f(a: ptr8, b: ptr16, c: ptr32, d: int, e: uint, g, h: float):\n"
                     "    return d\n")
    assert ("def f(a: cython.uchar[:], b: cython.ushort[:], c: cython.uint[:], "
            "d: cython.int, e: cython.uint, g,\n      h: float):\n") in out


def test_writing_into_a_buffer_does_not_untype_the_index():
    _, report = rewrite("from turbo import turbo\n@turbo.viper\n"
                        "def f(out: ptr8, n: int):\n"
                        "    for i in range(n):\n"
                        "        out[i] = i\n")
    assert report["f"]["typed"] == ["i"]


def test_locals_that_depend_on_each_other_are_typed_together():
    _, report = rewrite("from turbo import turbo\n@turbo.viper\n"
                        "def f(n: int):\n"
                        "    x = 0\n    y = 1\n"
                        "    while x < n:\n"
                        "        x = y + 1\n        y = x * 2\n")
    assert report["f"]["typed"] == ["x", "y"]


def test_one_doubtful_value_untypes_everything_that_reads_it():
    _, report = rewrite("from turbo import turbo\n@turbo.viper\n"
                        "def f(n: int):\n"
                        "    q = n / 2\n"
                        "    r = q + 1\n"
                        "    s = n // 2\n")
    assert report["f"] == {"typed": ["s"], "untyped": ["q", "r"], "casts": []}


def test_what_stays_untyped():
    _, report = rewrite("from turbo import turbo\n@turbo.viper\n"
                        "def f(buf: ptr8, n: int, thing):\n"
                        "    a, b = 1, 2\n"
                        "    ratio = 1.5\n"
                        "    flag = n > 3\n"
                        "    got = thing.value\n"
                        "    for z in buf:\n"
                        "        pass\n"
                        "    p = ptr8(buf)\n"
                        "    size = len(buf)\n")
    assert report["f"] == {"typed": ["size"],
                           "untyped": ["a", "b", "ratio", "flag", "got", "z", "p"],
                           "casts": ["ptr8"]}


def test_a_nested_scope_keeps_its_own_names():
    _, report = rewrite("from turbo import turbo\n@turbo.viper\n"
                        "def f(n: int):\n"
                        "    t = 0\n"
                        "    squares = [j * j for j in range(n)]\n"
                        "    def inner():\n"
                        "        hidden = 1\n"
                        "    return t\n")
    assert report["f"] == {"typed": ["t"], "untyped": ["squares"], "casts": []}


def test_a_global_is_never_typed():
    _, report = rewrite("from turbo import turbo\n@turbo.viper\n"
                        "def f():\n"
                        "    global count\n"
                        "    count = 0\n")
    assert report["f"] == {"typed": [], "untyped": ["count"], "casts": []}


def test_native_and_bare_turbo_only_lose_the_decorator():
    out, report = rewrite("from turbo import turbo\n\n@turbo\ndef g(x):\n    return x + 1\n\n"
                          "@turbo.native\ndef h(x): return x\n")
    assert out == "import cython\n\ndef g(x):\n    return x + 1\n\ndef h(x): return x\n"
    assert report == {}


def test_a_method_keeps_its_indent():
    out, _ = rewrite("from turbo import turbo\n\nclass K:\n"
                     "    @turbo.viper\n"
                     "    def f(self, n: int):\n"
                     "        t = n\n")
    assert ("class K:\n"
            "    @cython.boundscheck(False)\n"
            "    @cython.wraparound(False)\n"
            "    @cython.cdivision(True)\n"
            "    @cython.locals(t=cython.int)\n"
            "    def f(self, n: cython.int):\n"
            "        t = n\n") in out


def test_a_split_header_is_rejoined_and_comments_in_the_body_survive():
    out, _ = rewrite("from turbo import turbo\n@turbo.viper\n"
                     "def f(a: int,\n"
                     "      b: int):  # two of them\n"
                     "    # keep me\n"
                     "    return a + b\n")
    assert "def f(a: cython.int, b: cython.int):\n    # keep me\n    return a + b\n" in out


def test_long_lines_wrap_at_the_width():
    names = ["local_number_%d" % n for n in range(8)]
    out, _ = rewrite("from turbo import turbo\n@turbo.viper\ndef f():\n"
                     + "".join("    %s = 0\n" % n for n in names))
    assert max(len(line) for line in out.splitlines()) <= t.CY_WIDTH
    assert all(n + "=cython.int" in out for n in names)
