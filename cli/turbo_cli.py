#!/usr/bin/env python3
"""turbo_cli: compile @turbo-decorated CircuitPython modules to native .mpy,
bench the candidates on a board, install the winner, keep a manifest.

    turbo_cli.py build  SRC_DIR [--out lib/turbo] [--mpy-cross PATH] [--arch a,b]
    turbo_cli.py bench  MODULE --port TTY --mount CIRCUITPY [--out lib/turbo] [--trials N]
    turbo_cli.py check  SRC_DIR [--out lib/turbo]
    turbo_cli.py analyze SRC [--arch A | --board B] [--json]
    turbo_cli.py pack   PROJECT --board B --firmware FW.uf2 [-o out.uf2]
    turbo_cli.py pack   PROJECT --self-extract [-o code.py]

Conventions: a module opts in with `from turbo import turbo` and `@turbo`,
`@turbo.native` or `@turbo.viper` on functions. It may define `_turbo_bench()`
returning a comparable value; bench times it and rejects variants whose value
differs from bytecode. The shim (lib/turbo.py) picks the arch dir at runtime.
"""
import argparse
import ast
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

ARCHES = ["armv6m", "armv7emsp"]
ARCH_ID = {"armv6m": 4, "armv7m": 5, "armv7em": 6, "armv7emsp": 7, "armv7emdp": 8,
           "xtensa": 9, "xtensawin": 10, "rv32imc": 11}
DECO = re.compile(r"^(\s*)@turbo(\.native|\.viper)?\s*$")

# ---------------------------------------------------------------- board facts
# Pure functions, no serial. Sources: SPEC.md 2.1 to 2.4 and 7.1.

ARCH_NAME = {v: k for k, v in ARCH_ID.items()}  # 0 is absent: no native loader

MPY_CROSS_BASE = "https://adafruit-circuit-python.s3.amazonaws.com/bin/mpy-cross/"
# platform key -> path under MPY_CROSS_BASE, with %s for the CircuitPython version
MPY_CROSS_PATH = {
    "macos-arm64": "macos/mpy-cross-macos-%s-arm64",
    "linux-amd64": "linux-amd64/mpy-cross-linux-amd64-%s.static",
    "linux-aarch64": "linux-aarch64/mpy-cross-linux-aarch64-%s.static-aarch64",
    "linux-raspbian": "linux-raspbian/mpy-cross-linux-raspbian-%s.static-raspbian",
    "windows": "windows/mpy-cross-windows-%s.static.exe",
}


def decode_mpy(mpy):
    """Decode sys.implementation._mpy (py/persistentcode.h). Returns a dict with
    version, sub, arch_id, arch (name, or None when arch_id is 0: no native loader)
    and abi ("6.3")."""
    version, sub, arch_id = mpy & 0xff, (mpy >> 8) & 3, (mpy >> 10) & 0x3f
    return {"version": version, "sub": sub, "arch_id": arch_id,
            "arch": ARCH_NAME.get(arch_id), "abi": "%d.%d" % (version, sub)}


def parse_boot_out(text):
    """Parse boot_out.txt (main.c:880). Only the first lines matter; boot.py output
    may follow. Returns version, build_date, board_name, machine, board_id, uid;
    each None when absent. Same split as circup backends.py:257."""
    lines = text.splitlines()
    facts = dict.fromkeys(("version", "build_date", "board_name", "machine", "board_id", "uid"))
    if lines and lines[0].startswith("Adafruit CircuitPython "):
        head, _, tail = lines[0].partition(";")
        words = head.split(" ")
        if len(words) >= 5:
            facts["version"], facts["build_date"] = words[-3], words[-1]
        name, sep, machine = tail.strip().rpartition(" with ")
        facts["board_name"] = name if sep else (tail.strip() or None)
        facts["machine"] = machine if sep else None
    for line in lines[1:3]:
        if line.startswith("Board ID:"):
            facts["board_id"] = line[9:].strip() or None
        elif line.startswith("UID:"):
            facts["uid"] = line[4:].strip() or None
    return facts


def read_boot_out(mount):
    """boot_out.txt facts for a mount, or None if the file is unreadable."""
    try:
        with open(os.path.join(mount, "boot_out.txt"), errors="replace") as f:
            return parse_boot_out(f.read())
    except OSError:
        return None


def _mount_candidates(system):
    env = os.environ.get("CIRCUITPY_MOUNT")
    if env:
        yield env
    if system == "Darwin":
        # macOS names a second board "CIRCUITPY 1"
        for p in sorted(glob.glob("/Volumes/CIRCUITPY*")):
            yield p
    elif system == "Linux":
        for pat in ("/media/*/CIRCUITPY", "/run/media/*/CIRCUITPY", "/mnt/CIRCUITPY"):
            for p in sorted(glob.glob(pat)):
                yield p
    elif system == "Windows":
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            root = letter + ":\\"
            if os.path.exists(root) and _volume_label(root) == "CIRCUITPY":
                yield root


def _volume_label(root):
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(root, buf, 261, None, None, None,
                                                          None, 0)
        return buf.value if ok else None
    except Exception:
        return None


def find_mounts(explicit=None, board=None, system=None):
    """CIRCUITPY drives (SPEC 7.1), preferred first. A path counts only if it holds
    a readable boot_out.txt. `explicit` (--mount) is the only candidate when given.
    With `board`, a drive whose Board ID matches moves to the front."""
    system = system or platform.system()
    cands = [explicit] if explicit else list(_mount_candidates(system))
    found, seen = [], set()
    for p in cands:
        p = os.path.normpath(p)
        if p in seen:
            continue
        seen.add(p)
        facts = read_boot_out(p)
        if facts:
            found.append((p, facts))
    if board:
        found.sort(key=lambda pf: pf[1]["board_id"] != board)
    return found


def platform_key(system=None, machine=None):
    """Key into MPY_CROSS_PATH for this host, or None when Adafruit publishes no
    binary (macOS x86_64, and anything else unlisted)."""
    system = system or platform.system()
    machine = machine or platform.machine()
    if system == "Darwin" and machine == "arm64":
        return "macos-arm64"
    if system == "Linux":
        return {"x86_64": "linux-amd64", "aarch64": "linux-aarch64",
                "armv7l": "linux-raspbian"}.get(machine)
    if system == "Windows" and machine == "AMD64":
        return "windows"
    return None


def mpy_cross_url(version, key):
    """Download URL for the official mpy-cross of CircuitPython `version` on host
    platform `key` (see platform_key). Beta/RC/dev versions are not published;
    the caller learns that from the 404, not from this function."""
    return MPY_CROSS_BASE + MPY_CROSS_PATH[key] % version



def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def rewrite(src_text, tier):
    """Return source with @turbo lines rewritten for `tier` ('native'|'viper'),
    or None if the module has no @turbo decorators.

    viper tier: @turbo.viper -> @micropython.viper, everything else -> native.
    native tier: every @turbo* -> @micropython.native.
    """
    out, n = [], 0
    for line in src_text.splitlines(keepends=True):
        m = DECO.match(line)
        if m:
            n += 1
            want = "viper" if (tier == "viper" and m.group(2) == ".viper") else "native"
            line = "%s@micropython.%s\n" % (m.group(1), want)
        out.append(line)
    return "".join(out) if n else None


def compile_variant(mpy_cross, text, name, arch, dest):
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, name + ".py")
        with open(src, "w") as f:
            f.write(text)
        r = subprocess.run([mpy_cross, "-march=" + arch, src, "-o", dest],
                           capture_output=True, text=True)
    if r.returncode:
        return r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "mpy-cross failed"
    return None


def load_manifest(out):
    p = os.path.join(out, "turbo.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}


def save_manifest(out, m):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "turbo.json"), "w") as f:
        json.dump(m, f, indent=2, sort_keys=True)
        f.write("\n")


def cmd_build(a):
    manifest = load_manifest(a.out)
    rc = 0
    for fn in sorted(os.listdir(a.src)):
        if not fn.endswith(".py"):
            continue
        name = fn[:-3]
        path = os.path.join(a.src, fn)
        text = open(path).read()
        if rewrite(text, "native") is None:
            print("%-14s no @turbo, skipped" % name)
            continue
        entry = manifest.setdefault(name, {})
        entry["src"] = os.path.relpath(path)
        # The hash is written only after every arch has a fresh candidate (below);
        # a failed build must leave `check` reporting STALE, not "fresh".
        new_sha = sha256(path)
        all_built = True
        for arch in a.arch.split(","):
            d = os.path.join(a.out, arch)
            os.makedirs(d, exist_ok=True)
            built = {}
            for tier in ("viper", "native"):
                dest = os.path.join(d, "%s.%s.mpy" % (name, tier))
                err = compile_variant(a.mpy_cross, rewrite(text, tier), name, arch, dest)
                if err:
                    built[tier] = "failed: " + err
                    if os.path.exists(dest):
                        os.remove(dest)
                else:
                    built[tier] = os.path.getsize(dest)
            # install a default until bench picks: viper if it compiled, else native
            pick = "viper" if isinstance(built.get("viper"), int) else \
                   "native" if isinstance(built.get("native"), int) else None
            arch_entry = entry.setdefault(arch, {})
            arch_entry["candidates"] = built
            # Whatever happens, the previous binary's measurements describe a
            # binary that no longer exists.
            arch_entry["measured"] = False
            arch_entry.pop("bench", None)
            arch_entry.pop("speedup_vs_bytecode", None)
            installed_path = os.path.join(d, name + ".mpy")
            if pick:
                shutil.copyfile(os.path.join(d, "%s.%s.mpy" % (name, pick)), installed_path)
                arch_entry["installed"] = pick
            else:
                # Do not leave an old binary standing in for the new source.
                if os.path.exists(installed_path):
                    os.remove(installed_path)
                arch_entry["installed"] = None
                all_built = False
                rc = 1
            print("%-14s %-10s %s -> installed %s" % (name, arch, built, pick))
        if all_built:
            entry["sha256"] = new_sha
    save_manifest(a.out, manifest)
    return rc


def board_exec(pyb, code, timeout=600):
    return pyb.exec_(code, timeout=timeout).decode().strip()


def cmd_bench(a):
    sys.path.insert(0, a.pyboard_tools)
    import pyboard  # from a CircuitPython/MicroPython tree

    manifest = load_manifest(a.out)
    entry = manifest.get(a.module)
    if not entry:
        sys.exit("no manifest entry for %s; run build first" % a.module)

    pyb = pyboard.Pyboard(a.port, 115200)
    pyb.enter_raw_repl()
    mpy = int(board_exec(pyb, "import sys; print(sys.implementation._mpy)"))
    arch = {v: k for k, v in ARCH_ID.items()}.get(mpy >> 10)
    print("board: mpy v%d.%d arch %s" % (mpy & 0xff, (mpy >> 8) & 3, arch))
    if not arch:
        sys.exit("stock firmware, nothing to bench")
    board_dir = os.path.join(a.mount, "lib", "turbo", arch)
    os.makedirs(board_dir, exist_ok=True)
    installed = os.path.join(board_dir, a.module + ".mpy")

    variants = {"bytecode": ("py", entry["src"])}
    for tier, val in entry.get(arch, {}).get("candidates", {}).items():
        if isinstance(val, int):
            variants[tier] = ("mpy", os.path.join(a.out, arch, "%s.%s.mpy" % (a.module, tier)))

    def clear_board_module():
        for f in os.listdir(board_dir):
            if f.startswith(a.module + "."):
                os.remove(os.path.join(board_dir, f))

    # Snapshot the module's files under the arch dir so an interrupted bench can
    # put the board back exactly as it was.
    before = {f: open(os.path.join(board_dir, f), "rb").read()
              for f in os.listdir(board_dir) if f.startswith(a.module + ".")}
    done = False
    results = {}
    try:
        for tier, (kind, src) in variants.items():
            # place exactly one candidate under the arch dir, as the name the shim imports
            clear_board_module()
            shutil.copyfile(src, os.path.join(board_dir, a.module + (".py" if kind == "py" else ".mpy")))
            os.sync()
            time.sleep(2)
            pyb.exit_raw_repl()
            pyb.enter_raw_repl()  # soft reset clears the module cache
            board_exec(pyb, "import gc, time, turbo, %s" % a.module)
            vals, times = set(), []
            for _ in range(a.trials):
                board_exec(pyb, "gc.collect()")
                out = board_exec(pyb, "t0=time.monotonic_ns(); v=%s._turbo_bench(); "
                                      "print((time.monotonic_ns()-t0)//1000, v)" % a.module).split()
                times.append(int(out[0]))
                vals.add(out[1])
            times.sort()
            results[tier] = {"us_median": times[len(times) // 2], "us_min": times[0],
                             "value": sorted(vals)[0] if len(vals) == 1 else list(vals)}
            print("%-9s median %9.1f ms  value %s" % (tier, times[len(times) // 2] / 1000, results[tier]["value"]))

        ref = results["bytecode"]["value"]
        base = results["bytecode"]["us_median"]
        ok = {t: r for t, r in results.items() if t != "bytecode" and r["value"] == ref}
        bad = [t for t in results if t != "bytecode" and t not in ok]
        if bad:
            print("rejected (output differs from bytecode):", ", ".join(bad))
        # Bytecode is a candidate too. A compiled tier wins only if it beats the
        # bytecode median by --min-gain; otherwise the source stays and no .mpy
        # is installed for this module (the shim then imports it from /src).
        best = min(ok, key=lambda t: ok[t]["us_median"]) if ok else None
        gain = base / ok[best]["us_median"] if best else None
        if best and gain >= a.min_gain:
            winner, why = best, None
        elif best:
            winner, why = None, "bytecode wins: %s is %.2fx, below --min-gain %.2f" % (best, gain, a.min_gain)
        else:
            winner, why = None, "bytecode wins: no compiled candidate matched the bytecode output"
        if why:
            print(why)

        clear_board_module()
        local_installed = os.path.join(a.out, arch, a.module + ".mpy")
        if winner:
            shutil.copyfile(os.path.join(a.out, arch, "%s.%s.mpy" % (a.module, winner)), installed)
            shutil.copyfile(installed, local_installed)
        elif os.path.exists(local_installed):
            os.remove(local_installed)  # so pack does not ship a loser
        os.sync()
        done = True
    finally:
        if not done:
            # interrupted or failed: restore the module's files on the board
            try:
                clear_board_module()
                for f, data in before.items():
                    with open(os.path.join(board_dir, f), "wb") as fh:
                        fh.write(data)
                os.sync()
                print("bench aborted; board files for %s restored" % a.module)
            except OSError as e:
                print("bench aborted; could not restore board files:", e)
        try:
            pyb.exit_raw_repl()
        except Exception:
            pass
        pyb.close()

    # reload before writing: another bench (other board, other arch) may have saved meanwhile
    manifest = load_manifest(a.out)
    entry = manifest.setdefault(a.module, entry)
    ae = entry.setdefault(arch, {})
    ae.update({"installed": winner, "measured": True, "mpy_abi": "%d.%d" % (mpy & 0xff, (mpy >> 8) & 3),
               "bench": results, "rejected": bad, "trials": a.trials, "min_gain": a.min_gain,
               "speedup_vs_bytecode": round(gain, 2) if winner else None,
               "bytecode_wins": why})
    save_manifest(a.out, manifest)
    if winner:
        print("installed %s for %s (%.2fx over bytecode)" % (winner, arch, gain))
    else:
        print("installed nothing for %s; %s imports from /src" % (arch, a.module))


def cmd_check(a):
    manifest = load_manifest(a.out)
    rc = 0
    for name, entry in sorted(manifest.items()):
        path = os.path.join(a.src, name + ".py")
        if not os.path.exists(path):
            print("%-14s source missing" % name)
            rc = 1
            continue
        fresh = sha256(path) == entry.get("sha256")
        archs = ", ".join("%s=%s%s" % (k, v.get("installed"), "" if v.get("measured") else "?")
                          for k, v in entry.items() if isinstance(v, dict))
        print("%-14s %s  %s" % (name, "fresh" if fresh else "STALE, rebuild", archs))
        rc |= not fresh
    return rc


def cmd_pack(a):
    """Stage the project, drop unpicked candidates, hand to folder2uf2."""
    if shutil.which(a.folder2uf2) is None:
        sys.exit("%s not found; pip install folder2uf2" % a.folder2uf2)
    lib_turbo = os.path.join(a.project, "lib", "turbo")
    if not os.path.isfile(os.path.join(a.project, "lib", "turbo.py")):
        print("warning: no lib/turbo.py in project; the shim will not be on the board")
    manifest = load_manifest(lib_turbo)
    stale = []
    for name, entry in manifest.items():
        src = os.path.join(a.project, entry.get("src", ""))
        if not os.path.isfile(src) or sha256(src) != entry.get("sha256"):
            stale.append(name)
    if stale and not a.force:
        sys.exit("stale compiled modules (source changed since build): %s\n"
                 "run `turbo build` again, or pass --force" % ", ".join(stale))

    with tempfile.TemporaryDirectory() as td:
        stage = os.path.join(td, "stage")
        shutil.copytree(a.project, stage, ignore=shutil.ignore_patterns(
            ".git", ".DS_Store", "__pycache__", "*.uf2"))
        dropped = 0
        st = os.path.join(stage, "lib", "turbo")
        if os.path.isdir(st):
            for arch in os.listdir(st):
                d = os.path.join(st, arch)
                if not os.path.isdir(d):
                    continue
                for f in os.listdir(d):
                    if f.endswith(".native.mpy") or f.endswith(".viper.mpy"):
                        os.remove(os.path.join(d, f))
                        dropped += 1
        if a.self_extract:
            out = a.output or "turbo-code.py"
            cmd = [a.folder2uf2, "--self-extract", "-o", out, stage]
        else:
            if not a.board or not a.firmware:
                sys.exit("pack needs --board and --firmware, or --self-extract")
            out = a.output or "%s-turbo.uf2" % a.board
            cmd = [a.folder2uf2, "--board", a.board, "--combine", a.firmware, "-o", out, stage]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.stdout.strip():
            print(r.stdout.strip())
        if r.returncode:
            sys.exit(r.stderr.strip() or "folder2uf2 failed")
    print("packed %s (%d bytes), %d candidate files dropped, %d compiled modules"
          % (out, os.path.getsize(out), dropped, len(manifest)))


# ---------------------------------------------------------------- analyze

# Measured on the farm, docs/shim-test.md. No entry means no number is printed;
# never interpolate a speedup for an arch we have not run.
MEASURED = {
    "armv6m":    {"viper": 19.7, "native": None, "board": "Metro RP2040"},
    "armv7emsp": {"viper": 16.3, "native": None, "board": "Metro RP2350"},
    "xtensawin": {"viper": 26.2, "native": 2.85, "board": "Metro ESP32-S3"},
}
BOARD_ARCH = {
    "metro_m0_express": "armv6m",
    "adafruit_metro_rp2040": "armv6m",
    "metro_m4_airlift_lite": "armv7emsp",
    "adafruit_metro_rp2350": "armv7emsp",
    "adafruit_feather_nrf52840_express": "armv7emsp",
    "adafruit_feather_stm32f405_express": "armv7emsp",
    "adafruit_metro_esp32s2": "xtensawin",
    "adafruit_metro_esp32s3": "xtensawin",
}

_INT_OPS = (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod,
            ast.LShift, ast.RShift, ast.BitAnd, ast.BitOr, ast.BitXor)
_BUFFERS = {"bytearray", "bytes", "array", "memoryview"}
_IO_ROOTS = {"board", "digitalio", "analogio", "busio", "pwmio", "touchio", "rotaryio",
             "countio", "neopixel", "displayio", "framebufferio", "terminalio",
             "audiocore", "audiobusio", "audiopwmio", "storage", "microcontroller",
             "usb_cdc", "usb_hid", "wifi", "socketpool", "ssl", "supervisor"}
_SAFE_CALLS = {"len", "range", "int", "abs", "min", "max", "ord", "chr", "bool"}
_STR_METHODS = {"join", "format", "split", "strip", "encode", "decode", "replace",
                "startswith", "endswith", "upper", "lower"}
_GROW = {"append", "extend", "insert", "pop", "remove", "add", "update", "setdefault"}
_TRANSCENDENTAL = {"sin", "cos", "tan", "asin", "acos", "atan", "atan2", "exp", "log",
                   "log2", "log10", "sqrt", "pow", "hypot", "degrees", "radians"}
VERDICT_RANK = {"viper": 3, "fixed-point": 2, "native": 1, "skip": 0}


def _dotted(node):
    """'time.sleep' for an Attribute/Name chain, '' for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return base + "." + node.attr if base else ""
    return ""


def _uniq(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


class _Signals(ast.NodeVisitor):
    """Shape of one function body: loop depth, arithmetic kind, viper blockers."""

    def __init__(self, fn):
        self.names = {a.arg for a in fn.args.args}
        self.buffers = set()
        for n in ast.walk(fn):
            if isinstance(n, (ast.Assign, ast.AugAssign, ast.For)):
                tgts = n.targets if isinstance(n, ast.Assign) else [
                    n.target if isinstance(n, ast.AugAssign) else n.target]
                for t in tgts:
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Name):
                            self.names.add(sub.id)
                val = getattr(n, "value", None)
                if isinstance(val, ast.Call) and _dotted(val.func) in _BUFFERS:
                    for t in tgts:
                        if isinstance(t, ast.Name):
                            self.buffers.add(t.id)
        self.depth = self.max_depth = 0
        self.int_ops = self.float_ops = self.index = 0
        self.blockers, self.io, self.transcendental = [], [], []

    def visit_For(self, node):
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        self.generic_visit(node)
        self.depth -= 1

    visit_While = visit_For
    visit_AsyncFor = visit_For

    def visit_BinOp(self, node):
        if isinstance(node.op, ast.Div):
            self.float_ops += 1
        elif isinstance(node.op, ast.Pow):
            self.blockers.append("** operator")
        elif isinstance(node.op, _INT_OPS):
            self.int_ops += 1
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self.visit_BinOp(node)

    def visit_Constant(self, node):
        if isinstance(node.value, float):
            self.float_ops += 1

    def visit_Subscript(self, node):
        base = node.value
        if isinstance(base, ast.Name) and base.id in self.names:
            self.index += 1
        self.generic_visit(node)

    def visit_Call(self, node):
        name = _dotted(node.func)
        root, leaf = name.split(".")[0], name.split(".")[-1]
        if root in _IO_ROOTS or root.startswith("adafruit_"):
            self.io.append(root)
        elif name in ("time.sleep", "time.monotonic", "print"):
            self.io.append(name)
        elif root == "math":
            self.float_ops += 1
            if leaf in _TRANSCENDENTAL:
                self.transcendental.append(name)
        elif name == "float":
            self.float_ops += 1
        elif leaf in _STR_METHODS:
            self.blockers.append("string work")
        elif leaf in _GROW and self.depth:
            self.blockers.append("%s() grows a container in the loop" % leaf)
        elif name and name not in _SAFE_CALLS and name not in _BUFFERS:
            self.blockers.append("calls %s()" % name)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        root = _dotted(node).split(".")[0]
        if self.depth and (root == "self" or root in self.names):
            self.blockers.append("object attribute %s" % _dotted(node))
        self.generic_visit(node)

    def visit_Try(self, node):
        self.blockers.append("try/except")
        self.generic_visit(node)

    def visit_Yield(self, node):
        self.blockers.append("generator")
        self.generic_visit(node)

    visit_YieldFrom = visit_Yield

    def visit_JoinedStr(self, node):
        self.blockers.append("f-string")
        self.generic_visit(node)

    def visit_ListComp(self, node):
        if self.depth:
            self.blockers.append("allocates in the loop")
        self.generic_visit(node)

    visit_DictComp = visit_ListComp
    visit_SetComp = visit_ListComp

    def visit_List(self, node):
        if self.depth:
            self.blockers.append("allocates in the loop")
        self.generic_visit(node)

    visit_Dict = visit_List


def classify(s):
    """(verdict, reason) for one function's signals. Conservative: anything we
    cannot see through lands in a lower bucket, never a higher one."""
    if s.max_depth == 0:
        return "skip", "no loop"
    if s.io:
        return "skip", "I/O bound (%s)" % ", ".join(_uniq(s.io)[:3])
    blockers = _uniq(s.blockers)
    if s.transcendental:
        return "native", "%s in the loop, stays object math" % _uniq(s.transcendental)[0]
    if blockers:
        return "native", "loop x%d, %s" % (s.max_depth, "; ".join(blockers[:2]))
    if s.float_ops:
        return "fixed-point", "float loop, converts to fixed point"
    if s.int_ops == 0:
        return "skip", "loop does no arithmetic"
    return "viper", "loop x%d, integer math%s" % (
        s.max_depth, ", buffer indexing" if s.index else "")


def _deco_tier(fn):
    for d in fn.decorator_list:
        name = _dotted(d.func if isinstance(d, ast.Call) else d)
        if name == "turbo" or name.startswith("turbo."):
            return name
    return None


def _hot_callees(tree):
    """Names called from inside some loop in this file. Weak hotness proxy."""
    hot, seen = set(), set()

    def walk(node, in_loop):
        for child in ast.iter_child_nodes(node):
            loop = in_loop or isinstance(node, (ast.For, ast.While, ast.AsyncFor))
            if isinstance(child, ast.Call):
                name = _dotted(child.func).split(".")[-1]
                seen.add(name)
                if loop:
                    hot.add(name)
            walk(child, loop)

    walk(tree, False)
    return hot, seen


def analyze_file(path, arch):
    with open(path) as f:
        text = f.read()
    tree = ast.parse(text, filename=path)
    hot, called = _hot_callees(tree)
    rows = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef):
            rows.append({"function": fn.name, "line": fn.lineno, "verdict": "skip",
                         "reason": "async function", "notes": [], "decorated": None})
            continue
        if not isinstance(fn, ast.FunctionDef):
            continue
        s = _Signals(fn)
        s.generic_visit(fn)
        verdict, reason = classify(s)
        notes = []
        m = MEASURED.get(arch) if arch else None
        speed = m.get({"viper": "viper", "fixed-point": "viper", "native": "native"}.get(verdict))\
            if m else None
        if verdict in ("viper", "fixed-point", "native"):
            if speed:
                notes.append("similar loops ran %.1fx on %s%s"
                             % (speed, m["board"],
                                " AFTER a hand rewrite" if verdict == "fixed-point" else ""))
            elif arch:
                notes.append("no measurement for %s yet" % arch)
            else:
                notes.append("pass --arch or --board for measured numbers")
        if verdict == "fixed-point":
            notes.append("analyze will not do the rewrite; see "
                         "examples/mandelbrot/src/pixels.py")
        if verdict in ("viper", "fixed-point") and fn.name not in hot:
            notes.append("no hot call site found in this file"
                         if fn.name in called else "never called in this file")
        tier = _deco_tier(fn)
        if tier:
            notes.append("already marked @%s" % tier)
        rows.append({"function": fn.name, "line": fn.lineno, "verdict": verdict,
                     "reason": reason, "notes": notes, "decorated": tier})
    rows.sort(key=lambda r: (-VERDICT_RANK[r["verdict"]], r["line"]))
    return rows


def cmd_analyze(a):
    arch = a.arch
    if a.board:
        arch = BOARD_ARCH.get(a.board)
        if not arch:
            return print("unknown board %s; known: %s, or use --arch"
                         % (a.board, ", ".join(sorted(BOARD_ARCH)))) or 2
    if arch and arch not in ARCH_ID:
        return print("unknown arch %s; known: %s" % (arch, ", ".join(sorted(ARCH_ID)))) or 2
    if os.path.isdir(a.src):
        paths = sorted(os.path.join(r, f) for r, _, fs in os.walk(a.src) for f in fs
                       if f.endswith(".py") and "lib" not in r.split(os.sep))
    else:
        paths = [a.src]
    results = []
    for p in paths:
        try:
            results.append((p, analyze_file(p, arch)))
        except SyntaxError as e:
            results.append((p, [{"function": "-", "line": e.lineno or 0, "verdict": "skip",
                                 "reason": "syntax error: %s" % e.msg, "notes": [],
                                 "decorated": None}]))
    if a.json:
        print(json.dumps([{"file": p, "arch": arch, **r} for p, rows in results for r in rows],
                         indent=2))
        return 0
    print("turbo analyze reads shape only. It cannot see where time is actually")
    print("spent: a perfect-looking function that runs once is 0x. Measure with bench.")
    if arch:
        m = MEASURED.get(arch)
        print("target: %s%s" % (arch, " (%s)" % m["board"] if m else " (no farm data)"))
    for p, rows in results:
        if not rows:
            continue
        print("\n%s" % p)
        for r in rows:
            print("  %-16s %-12s %s" % (r["function"], r["verdict"], r["reason"]))
            for n in r["notes"]:
                print("  %-16s %-12s %s" % ("", "", n))
    return 0



# ---------------------------------------------------------------- doctor

L = 12  # label column


def cache_root():
    return os.environ.get("TURBO_CACHE") or os.path.join(os.path.expanduser("~"),
                                                         ".cache", "turbo")


def is_release_version(version):
    """Adafruit publishes mpy-cross per release only, so a version string that is not
    exactly N.N.N (beta, rc, or a -N-ghash[-dirty] dev build) has no binary to fetch."""
    return bool(version and re.match(r"^\d+\.\d+\.\d+$", version))


def cached_mpy_cross(version, key):
    """Path to the cached official mpy-cross for this version and host, or None."""
    if not version or not key:
        return None
    p = os.path.join(cache_root(), "mpy-cross", version, key,
                     "mpy-cross.exe" if key == "windows" else "mpy-cross")
    return p if os.path.isfile(p) else None


def mpy_cross_abi(path):
    """(CircuitPython version, mpy abi) an mpy-cross binary reports, or (None, None).
    It prints e.g. "CircuitPython 10.3.0 on 2026-08-31; mpy-cross emitting mpy v6.3"."""
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None, None
    m = re.search(r"CircuitPython (\S+).*?mpy v(\d+\.\d+)", r.stdout + r.stderr, re.S)
    return (m.group(1), m.group(2)) if m else (None, None)


def project_state(out, arch, src="src"):
    """(module count, stale count) for `arch` in the manifest, or None with no project."""
    if not os.path.isdir(src) or not arch:
        return None
    manifest = load_manifest(out)
    n = stale = 0
    for name, entry in manifest.items():
        if not isinstance(entry.get(arch), dict):
            continue
        n += 1
        path = entry.get("src", "")
        if not os.path.isfile(path) or sha256(path) != entry.get("sha256"):
            stale += 1
    return (n, stale) if n else None


def board_facts(a):
    """Everything doctor reports, gathered once. Never raises: an unreachable board
    is a fact, not an error. arch_source says how much to trust `arch`."""
    f = {"mount": None, "mounts": 0, "boot": {}, "port": None, "port_errors": [],
         "mpy": None, "arch": None, "abi": None, "arch_source": None}
    mounts = find_mounts(getattr(a, "mount", None), getattr(a, "board", None))
    f["mounts"] = len(mounts)
    if mounts:
        f["mount"], f["boot"] = mounts[0]
    try:
        import turbo_repl
        ports = turbo_repl.find_ports(getattr(a, "port", None), f["boot"].get("uid"))
        port, mpy, version, errs = turbo_repl.probe_any(ports)
        f["port"], f["port_errors"] = port, errs
        if port is not None:
            f["mpy"] = mpy
            d = decode_mpy(mpy)
            f["arch"], f["abi"], f["arch_source"] = d["arch"], d["abi"], "probe"
            f["boot"].setdefault("version", version)
    except Exception as e:  # no pyserial, or the port vanished mid-probe
        f["port_errors"].append((None, str(e)))
    if f["arch_source"] is None and f["boot"].get("board_id"):
        f["arch"] = BOARD_ARCH.get(f["boot"]["board_id"])
        f["arch_source"] = "board_id" if f["arch"] else None
    if getattr(a, "arch", None):
        f["arch"], f["arch_source"] = a.arch, "flag"
    return f


def doctor_lines(f, mpy_cross=None, offline=False, out="lib/turbo", src="src"):
    """(lines, ready). ready is "could a build run right now" and drives the exit
    status. Wording and column widths are SPEC.md 5.1 and 6."""
    def row(label, value):
        lines.append("%-*s%s" % (L, label, value))

    lines, ready = [], False
    boot = f["boot"]
    if not f["mount"] and not f["port"]:
        lines.append("no board found")
        lines.append("   No CIRCUITPY drive and no serial port. Plug the board in, or pass")
        lines.append("   --mount DIR and --port TTY, or --arch NAME to build without a board.")
        if not f["arch"]:
            return lines, False

    if boot.get("board_name") or boot.get("board_id"):
        row("board", "%-30s%s" % (boot.get("board_name") or "?", boot.get("board_id") or ""))
    if f["port"]:
        row("port", f["port"])
    if f["mount"]:
        row("drive", f["mount"] + ("  (%d found, using the first; pass --mount)" % f["mounts"]
                                   if f["mounts"] > 1 else ""))
    version = boot.get("version")
    if version:
        row("firmware", "CircuitPython " + version)

    if f["mpy"] is not None and f["arch"]:
        row("_mpy", "0x%04x   arch %s \u00b7 mpy %s \u00b7 native loader present"
            % (f["mpy"], f["arch"], f["abi"]))
    elif f["mpy"] is not None:
        row("_mpy", "0x%04x   arch 0, no native loader" % f["mpy"])
        lines.append("   This board runs stock CircuitPython %s. Compiled modules will not"
                     % (version or "?"))
        lines.append("   load. Your code still runs from /src as bytecode.")
        lines.append("   Flash turbo firmware for %s (see docs/build.md)."
                     % (boot.get("board_id") or "this board"))
        return lines, False
    elif f["arch_source"] == "flag":
        row("_mpy", "not read; --arch %s given (loader presence unknown)" % f["arch"])
    elif f["mount"]:
        row("_mpy", "no serial port found; arch from board id: %s (loader presence unknown)"
            % (f["arch"] or "unknown"))

    if not f["arch"]:
        lines.append("no arch")
        lines.append("   Board id %s is not in the table and no port answered."
                     % (boot.get("board_id") or "?"))
        lines.append("   Pass --arch NAME (%s)." % ", ".join(sorted(ARCH_ID)))
        return lines, False

    key = platform_key()
    if mpy_cross:
        cp_version, abi = mpy_cross_abi(mpy_cross)
        row("toolchain", "%s   %s" % (mpy_cross,
                                      "mpy v%s" % abi if abi else "--version unreadable"))
        ready = True
    elif key is None:
        lines.append("host %s %s   Adafruit builds macOS arm64 only"
                     % (platform.system(), platform.machine()))
        lines.append("   Rosetta (arch -arm64 is not available on Intel), or a local build:")
        lines.append("   make -C mpy-cross in a CircuitPython checkout, then --mpy-cross PATH.")
    elif not version:
        row("toolchain", "no firmware version; cannot pick an mpy-cross")
    elif not is_release_version(version):
        lines.append("firmware %s   no published mpy-cross for that version" % version)
        lines.append("   Adafruit publishes mpy-cross per release only. Use a release build,")
        lines.append("   or point turbo at a local mpy-cross with --mpy-cross PATH.")
    else:
        cached = cached_mpy_cross(version, key)
        if cached:
            cp_version, abi = mpy_cross_abi(cached)
            if abi and f["abi"] and abi != f["abi"]:
                lines.append("mpy-cross %s reports mpy v%s, board wants v%s; refusing to use it"
                             % (version, abi, f["abi"]))
            else:
                row("toolchain", "%s   %d KB   mpy v%s"  # KB = 1000, as in SPEC 5.1
                    % (cached, os.path.getsize(cached) // 1000, abi or "?"))
                ready = True
        else:
            row("toolchain", "not cached  %s  %s" % (key, version))
            lines.append(" " * L + mpy_cross_url(version, key))

    p = project_state(out, f["arch"], src)
    if p:
        row("project", "%s/  %d module%s, %s" % (os.path.join(out, f["arch"]), p[0],
                                                 "" if p[0] == 1 else "s",
                                                 "fresh" if not p[1] else "%d stale" % p[1]))
    if ready:
        row("ready", "turbo build compiles -march=%s" % f["arch"])
    else:
        row("not ready", "no mpy-cross for CircuitPython %s on this host; pass --mpy-cross PATH"
            % (version or "?"))

    return lines, ready


def cmd_doctor(a):
    f = board_facts(a)
    lines, ready = doctor_lines(f, a.mpy_cross, a.offline, a.out)
    if a.json:
        print(json.dumps({"board": f["boot"].get("board_id"),
                          "board_name": f["boot"].get("board_name"),
                          "port": f["port"], "drive": f["mount"],
                          "firmware": f["boot"].get("version"), "mpy": f["mpy"],
                          "arch": f["arch"], "arch_source": f["arch_source"],
                          "mpy_abi": f["abi"], "mpy_cross": a.mpy_cross or cached_mpy_cross(
                              f["boot"].get("version"), platform_key()),
                          "ready": ready}, indent=2, sort_keys=True))
    else:
        print("\n".join(lines))
        if a.verbose:
            for port, err in f["port_errors"]:
                print("%-*s%s%s" % (L, "", "%s: " % port if port else "", err))
    return 0 if ready else 1



def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="board, firmware, arch and toolchain in one screen")
    d.add_argument("--port", help="serial port; autodetected when absent")
    d.add_argument("--mount", help="CIRCUITPY drive; autodetected when absent")
    d.add_argument("--board", help="board id, to pick between two drives")
    d.add_argument("--arch", help="assume this arch instead of asking the board")
    d.add_argument("--mpy-cross", help="use this mpy-cross instead of the cached one")
    d.add_argument("--out", default="lib/turbo")
    d.add_argument("--offline", action="store_true", help="never fetch")
    d.add_argument("--json", action="store_true")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(fn=cmd_doctor)
    b = sub.add_parser("build")
    b.add_argument("src")
    b.add_argument("--out", default="lib/turbo")
    b.add_argument("--mpy-cross", default="mpy-cross")
    b.add_argument("--arch", default=",".join(ARCHES))
    b.set_defaults(fn=cmd_build)
    n = sub.add_parser("bench")
    n.add_argument("module")
    n.add_argument("--port", required=True)
    n.add_argument("--mount", required=True)
    n.add_argument("--out", default="lib/turbo")
    n.add_argument("--trials", type=int, default=5)
    n.add_argument("--min-gain", type=float, default=1.05,
                   help="a compiled tier must beat the bytecode median by this factor (default 1.05)")
    n.add_argument("--pyboard-tools", default=os.path.expanduser("~/cp-1030/tools"))
    n.set_defaults(fn=cmd_bench)
    c = sub.add_parser("check")
    c.add_argument("src")
    c.add_argument("--out", default="lib/turbo")
    c.set_defaults(fn=cmd_check)
    k = sub.add_parser("pack", help="one UF2: turbo firmware + project files (or a self-extracting code.py)")
    k.add_argument("project", help="folder with code.py, lib/, src/")
    k.add_argument("--board", help="folder2uf2 board name, e.g. adafruit_metro_rp2350")
    k.add_argument("--firmware", help="turbo firmware .uf2 to combine with")
    k.add_argument("-o", "--output")
    k.add_argument("--self-extract", action="store_true",
                   help="emit a self-extracting code.py instead; works on any port, no firmware included")
    k.add_argument("--force", action="store_true", help="pack even if a compiled module is stale")
    k.add_argument("--folder2uf2", default="folder2uf2")
    k.set_defaults(fn=cmd_pack)
    z = sub.add_parser("analyze", help="static guess at which functions turbo can speed up")
    z.add_argument("src", help="a .py file or a project folder")
    z.add_argument("--arch", help="target arch, e.g. armv7emsp")
    z.add_argument("--board", help="board id, resolves to an arch")
    z.add_argument("--json", action="store_true")
    z.set_defaults(fn=cmd_analyze)
    a = p.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
