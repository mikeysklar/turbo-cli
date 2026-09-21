# turbo on CPython: Blinka on a Raspberry Pi or any Linux board. Installed with the
# CLI, so `import turbo` works with no path set and the project folder stays the one
# that is copied to a CircuitPython board (whose own shim is lib/turbo.py).
#
# Puts <project>/lib/turbo/cpython, where `turbo build --target cpython` leaves its
# .so files, at the front of sys.path with <project>/src right behind it, so a module
# with no built file still imports from source. The project is the folder of the
# script being run.
#
#   TURBO=numba    @turbo.viper becomes numba.njit(cache=True). No build step. A
#                  function numba cannot compile runs as plain Python, with one line.
#   TURBO=source   ignore built files, run everything from source.
import builtins
import functools
import os
import sys

arch = "cpython"
mode = os.environ.get("TURBO", "")
if mode not in ("", "numba", "source"):
    print("turbo: TURBO=%s is not numba or source, ignored" % mode)
    mode = ""


def _jit(f):
    try:
        import numba
    except ImportError:
        print("turbo: TURBO=numba but numba is not installed, %s runs from source" % f.__name__)
        return f
    run = [numba.njit(cache=True)(f)]

    @functools.wraps(f)
    def call(*args, **kwargs):
        try:
            return run[0](*args, **kwargs)
        except numba.core.errors.NumbaError as e:
            if run[0] is f:
                raise
            # numba types a function on its first call, before running any of it
            print("turbo: numba cannot compile %s, running it from source" % f.__name__)
            print("   %s" % str(e).strip().splitlines()[0])
            run[0] = f
            return f(*args, **kwargs)
    return call


class _Turbo:
    # @turbo, @turbo.native and @turbo.viper are markers for `turbo build`. Here they
    # change nothing, unless TURBO=numba.
    def __call__(self, f):
        return f

    def native(self, f):
        return f

    def viper(self, f):
        return _jit(f) if mode == "numba" else f


turbo = _Turbo()


def _project():
    main = getattr(sys.modules.get("__main__"), "__file__", None)
    return os.path.dirname(os.path.abspath(main)) if main else os.getcwd()


def _pick():
    root = _project()
    found = []
    if mode != "source":
        found.append(os.path.join(root, "lib", "turbo", arch))
    found.append(os.path.join(root, "src"))
    return [p for p in found if os.path.isdir(p)]


paths = _pick()
path = paths[0] if paths else None  # where a compiled module comes from, if any
for _p in reversed(paths):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

# Viper's type names. MicroPython never evaluates `out: ptr8`; CPython before 3.14
# does, when the function is defined. As casts they hand the buffer straight back.
for _name in ("ptr8", "ptr16", "ptr32", "ptr"):
    if not hasattr(builtins, _name):
        setattr(builtins, _name, lambda buf: buf)
if not hasattr(builtins, "uint"):
    builtins.uint = lambda value: int(value) & 0xFFFFFFFF
