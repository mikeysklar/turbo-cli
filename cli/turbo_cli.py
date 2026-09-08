#!/usr/bin/env python3
"""turbo_cli: compile @turbo-decorated CircuitPython modules to native .mpy,
bench the candidates on a board, install the winner, keep a manifest.

    turbo_cli.py doctor [--port TTY] [--mount DIR] [--arch A] [--mpy-cross PATH]
    turbo_cli.py init   [--arch A] [--example]
    turbo_cli.py build  SRC_DIR [--out lib/turbo] [--mpy-cross PATH] [--arch a,b]
    turbo_cli.py bench  MODULE --port TTY --mount CIRCUITPY [--out lib/turbo] [--trials N]
    turbo_cli.py check  SRC_DIR [--out lib/turbo]
    turbo_cli.py analyze SRC [--arch A | --board B] [--json]
    turbo_cli.py verify BASELINE.py CANDIDATE.py --fn NAME [--inputs FILE.py]
    turbo_cli.py pack   PROJECT --board B --firmware FW.uf2 [-o out.uf2]
    turbo_cli.py pack   PROJECT --self-extract [-o code.py]

Conventions: a module opts in with `from turbo import turbo` and `@turbo`,
`@turbo.native` or `@turbo.viper` on functions. It may define `_turbo_bench()`
returning a comparable value; bench times it and rejects variants whose value
differs from bytecode. The shim (lib/turbo.py) picks the arch dir at runtime.
"""
import argparse
import ast
import copy
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
import types
import urllib.error
import urllib.request

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
        # the whole thing: build wants the `File "...", line N` line too (SPEC 4.2)
        return r.stderr.strip() or "mpy-cross failed"
    return None


# Hints for the compiler's own errors, SPEC 6. Keyed by prefix because the text
# carries a variable name. No match prints nothing; a wrong hint is worse than none.
HINTS = [
    ("ViperTypeError: can't do binary op",
     ["a float reached a viper function; scale to integers"]),
    ("ViperTypeError: local",
     ["a value came from a Python object; declare the parameter type",
      "(out: ptr8, n: int) or convert with int()"]),
    ("SyntaxError: invalid micropython decorator",
     ["this firmware has no emitter; the decorator must go through turbo build,",
      "not run from source"]),
    ("ValueError: incompatible .mpy arch",
     ["(seen on the board) the .mpy is for a different arch; turbo doctor"]),
    ("ValueError: native code in .mpy unsupported",
     ["(seen on the board) stock firmware; see the arch 0 sentence above"]),
]


def hint_for(message):
    for prefix, lines in HINTS:
        if message.startswith(prefix):
            return lines
    return []


def compiler_error(stderr, src_path):
    """(location, message, hint) from mpy-cross stderr. The rewritten source has the
    same line numbering as the original, so the traceback's line number is the user's."""
    lines = [l for l in (stderr or "").strip().splitlines() if l.strip()]
    message = lines[-1] if lines else "mpy-cross failed"
    line_no = None
    for l in lines:
        m = re.search(r'File "[^"]*", line (\d+)', l)
        if m:
            line_no = int(m.group(1))
    return ("%s:%d" % (src_path, line_no) if line_no else src_path), message, hint_for(message)



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


def thousands(n):
    return format(n, ",d")


def build_module(mpy_cross, name, path, text, archs, out, echo=print):
    """Compile one module for every arch. Prints the report lines (SPEC 5.3) and
    returns (manifest entry fields, installed paths, ok). ok is False when some arch
    ended with nothing installable, which is what makes build exit 1."""
    entry, installed, ok = {}, [], True
    label = name
    for arch in archs:
        d = os.path.join(out, arch)
        os.makedirs(d, exist_ok=True)
        sizes, errors, candidates = {}, {}, {}
        for tier in ("viper", "native"):
            dest = os.path.join(d, "%s.%s.mpy" % (name, tier))
            err = compile_variant(mpy_cross, rewrite(text, tier), name, arch, dest)
            if err:
                errors[tier] = err
                candidates[tier] = "failed: " + err.strip().splitlines()[-1]
                if os.path.exists(dest):
                    os.remove(dest)
            else:
                sizes[tier] = candidates[tier] = os.path.getsize(dest)
        # install viper if it compiled, else native (existing rule)
        pick = "viper" if "viper" in sizes else "native" if "native" in sizes else None
        arch_entry = {"candidates": candidates, "installed": pick, "measured": False}
        installed_path = os.path.join(d, name + ".mpy")
        if pick:
            shutil.copyfile(os.path.join(d, "%s.%s.mpy" % (name, pick)), installed_path)
            installed.append((arch, installed_path))
        elif os.path.exists(installed_path):
            # never let an old binary stand in for source that no longer compiles
            os.remove(installed_path)
        entry[arch] = arch_entry

        if "viper" in errors:
            loc, message, hint = compiler_error(errors["viper"], path)
            echo("%-11s%-8s%s" % (label, "FAILED", loc))
            echo(" " * 11 + message)
            for h in hint:
                echo(" " * 11 + h)
            if "native" in sizes:  # the module still ships, just slower
                echo("%-11s%-8s%-10s%6s B   installed"
                     % ("", "native", arch, thousands(sizes["native"])))
        elif pick:
            line = "%-11s%-8s%-10s%6s B" % (label, pick, arch, thousands(sizes[pick]))
            other = "native" if pick == "viper" else None
            if other in sizes:
                line += "      %-8s %5s B" % (other, thousands(sizes[other]))
            echo(line)
        if not pick:
            ok = False
        label = ""
    return entry, installed, ok


def copy_to_board(mount, out, installed, echo=print):
    """Put the installed .mpy files and the manifest where the shim looks."""
    n = 0
    for arch, path in installed:
        d = os.path.join(mount, "lib", "turbo", arch)
        os.makedirs(d, exist_ok=True)
        shutil.copyfile(path, os.path.join(d, os.path.basename(path)))
        n += 1
    manifest = os.path.join(out, "turbo.json")
    if n and os.path.isfile(manifest):
        shutil.copyfile(manifest, os.path.join(mount, "lib", "turbo", "turbo.json"))
    if n and hasattr(os, "sync"):
        os.sync()
    return n


def cmd_build(a):
    t0 = time.monotonic()
    f = board_facts(a)
    if a.arch == "all":
        archs = sorted(ARCH_ID)
    elif a.arch:
        archs = [x.strip() for x in a.arch.split(",") if x.strip()]
    elif f["arch"]:
        archs = [f["arch"]]
    else:
        print("no board found")
        print("   No CIRCUITPY drive and no serial port. Plug the board in, or pass")
        print("   --mount DIR and --port TTY, or --arch NAME to build without a board.")
        return 1
    unknown = [x for x in archs if x not in ARCH_ID]
    if unknown:
        print("unknown arch %s" % ", ".join(unknown))
        print("   Known: %s, or --arch all." % ", ".join(sorted(ARCH_ID)))
        return 1
    if not os.path.isdir(a.src):
        print("no source directory %s" % a.src)
        print("   turbo build reads .py files from src/.   turbo init")
        return 1

    mpy_cross, lines = resolve_toolchain(f["boot"].get("version"), f["abi"],
                                         a.mpy_cross, a.offline)
    # a cached toolchain is not worth a line; a fetch or a failure is
    if a.verbose or not mpy_cross or any("fetching" in l for l in lines):
        for line in lines:
            print(line)
    if not mpy_cross:
        return 1

    manifest = load_manifest(a.out)
    built = failed = skipped = 0
    installed = []
    for fn in sorted(os.listdir(a.src)):
        if not fn.endswith(".py"):
            continue
        name = fn[:-3]
        path = os.path.join(a.src, fn)
        with open(path) as fh:
            text = fh.read()
        if rewrite(text, "native") is None:
            print("%-11s%-8s%s" % (name, "skipped", "no @turbo decorator"))
            skipped += 1
            continue
        entry = manifest.setdefault(name, {})
        entry["src"] = os.path.relpath(path)
        # The hash is written only when every arch built, so a failure leaves
        # `check` reporting STALE rather than "fresh".
        new_sha = sha256(path)
        arch_entries, module_installed, ok = build_module(mpy_cross, name, path, text,
                                                          archs, a.out)
        entry.update(arch_entries)
        installed += module_installed
        if ok:
            entry["sha256"] = new_sha
            built += 1
        else:
            entry.pop("sha256", None)
            failed += 1
    save_manifest(a.out, manifest)

    copied = 0
    if installed and f["mount"] and not a.no_copy:
        copied = copy_to_board(f["mount"], a.out, installed)
    ms = int((time.monotonic() - t0) * 1000)
    parts = ["%d built" % built]
    if failed:
        parts.append("%d failed" % failed)
    if skipped:
        parts.append("%d skipped" % skipped)
    parts.append("%d ms" % ms)
    if copied:
        parts.append("copied %d to %s" % (copied, f["mount"]))
    elif installed and not a.no_copy and not f["mount"]:
        parts.append("no CIRCUITPY drive, nothing copied")
    print(", ".join(parts))
    return 1 if failed else 0


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


class ToolchainError(Exception):
    """No usable mpy-cross. `lines` is what to print: the sentence, then the fix
    indented three spaces (SPEC 6)."""

    def __init__(self, lines):
        super().__init__(lines[0])
        self.lines = lines


def tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and path.startswith(home + os.sep) else path


def cached_versions(key):
    """Versions of mpy-cross already in the cache for this host, newest name last."""
    root = os.path.join(cache_root(), "mpy-cross")
    if not os.path.isdir(root):
        return []
    return sorted(v for v in os.listdir(root) if cached_mpy_cross(v, key))


def fetch_mpy_cross(version, key, timeout=30):
    """Download the official mpy-cross into the cache and return its path. Writes a
    temp file and renames, so an interrupted fetch never leaves a half binary in
    place. Raises ToolchainError with the sentence to print."""
    url = mpy_cross_url(version, key)
    d = os.path.join(cache_root(), "mpy-cross", version, key)
    dest = os.path.join(d, "mpy-cross.exe" if key == "windows" else "mpy-cross")
    os.makedirs(d, exist_ok=True)
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except urllib.error.HTTPError as e:
        os.path.exists(tmp) and os.remove(tmp)
        if e.code == 404:
            raise ToolchainError([
                "firmware %s   no published mpy-cross for that version" % version,
                "   Adafruit publishes mpy-cross per release only. Use a release build,",
                "   or point turbo at a local mpy-cross with --mpy-cross PATH."])
        raise ToolchainError(["mpy-cross download failed: HTTP %d" % e.code,
                              "   " + url,
                              "   Retry, or pass --mpy-cross PATH."])
    except (urllib.error.URLError, OSError) as e:
        os.path.exists(tmp) and os.remove(tmp)
        raise ToolchainError(["mpy-cross download failed: %s" % e,
                              "   " + url,
                              "   Check the network, or pass --mpy-cross PATH."])
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)
    return dest


def validate_mpy_cross(path, version, abi=None):
    """Check a fetched binary reports the version we asked for and, when the board
    told us one, the same .mpy abi. Returns None when good, else the lines to print.
    A binary that fails validation is deleted: it would emit the wrong format."""
    got_version, got_abi = mpy_cross_abi(path)
    if got_version is None:
        os.remove(path)
        return ["mpy-cross %s does not run on this host" % version,
                "   %s printed no version. Deleted." % tilde(path),
                "   Pass --mpy-cross PATH, or build one from a CircuitPython checkout."]
    if abi and got_abi != abi:
        os.remove(path)
        return ["mpy-cross %s reports mpy v%s, board wants v%s; refusing to use it"
                % (version, got_abi, abi),
                "   Different .mpy format. Deleted.",
                "   Pass --mpy-cross PATH with a matching build."]
    if got_version != version:
        os.remove(path)
        return ["mpy-cross at %s is %s, not %s; refusing to use it"
                % (tilde(path), got_version, version),
                "   The cache is keyed by version, so this file is wrong. Deleted.",
                "   Run turbo doctor again to fetch %s." % version]
    return None


def resolve_toolchain(version, abi=None, mpy_cross=None, offline=False):
    """(path, lines): the mpy-cross to compile with, and what to say about it. The
    path is None when no usable binary exists, and the lines then hold the sentence
    and the fix (SPEC 6, 8). doctor and build both go through here."""
    lines = []

    def row(label, value):
        lines.append("%-*s%s" % (L, label, value))

    if mpy_cross:
        # an explicit path is trusted; --version is only reported, never enforced
        _, got_abi = mpy_cross_abi(mpy_cross)
        row("toolchain", "%s   %s" % (mpy_cross,
                                      "mpy v%s" % got_abi if got_abi else "--version unreadable"))
        return mpy_cross, lines

    key = platform_key()
    if key is None:
        lines += ["host %s %s   Adafruit builds macOS arm64 only"
                  % (platform.system(), platform.machine()),
                  "   Rosetta (arch -arm64 is not available on Intel), or a local build:",
                  "   make -C mpy-cross in a CircuitPython checkout, then --mpy-cross PATH."]
        return None, lines
    if not version:
        row("toolchain", "no firmware version; cannot pick an mpy-cross")
        lines.append("   Adafruit publishes one mpy-cross per CircuitPython release, so the")
        lines.append("   board has to say which. Attach it, or pass --mpy-cross PATH.")
        return None, lines
    if not is_release_version(version):
        lines += ["firmware %s   no published mpy-cross for that version" % version,
                  "   Adafruit publishes mpy-cross per release only. Use a release build,",
                  "   or point turbo at a local mpy-cross with --mpy-cross PATH."]
        return None, lines

    cached, fetched = cached_mpy_cross(version, key), False
    if not cached and offline:
        row("toolchain", "not cached  %s  %s" % (key, version))
        lines.append(" " * L + mpy_cross_url(version, key))
        lines.append(" " * L + "--offline, so nothing was fetched; or pass --mpy-cross PATH")
        return None, lines
    if not cached:
        others = [v for v in cached_versions(key) if v != version]
        if others:
            # SPEC 6: a cached mpy-cross for another release is the wrong format
            lines.append("mpy-cross %s cached, board runs %s" % (others[-1], version))
            lines.append("   Different .mpy format. Fetching %s." % version)
        row("toolchain", "fetching mpy-cross  %s  %s" % (key, version))
        lines.append(" " * L + mpy_cross_url(version, key))
        try:
            cached = fetch_mpy_cross(version, key)
            fetched = True
        except ToolchainError as e:
            lines += e.lines
            return None, lines

    bad = validate_mpy_cross(cached, version, abi)
    if bad:
        return None, lines + bad
    if fetched:
        lines.append(" " * L + "cached  %s   %d KB"
                     % (tilde(cached), os.path.getsize(cached) // 1000))
    else:
        row("toolchain", "%s   %d KB   mpy v%s"  # KB = 1000, as in SPEC 5.1
            % (tilde(cached), os.path.getsize(cached) // 1000, mpy_cross_abi(cached)[1] or "?"))
    return cached, lines



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


def doctor_lines(f, mpy_cross=None, offline=False, out="lib/turbo", src="src", echo=None):
    """(lines, ready). ready is "could a build run right now" and drives the exit
    status. Wording and column widths are SPEC.md 5.1 and 6. `echo` prints each line
    as it is produced, so a slow fetch is not silent."""
    lines, ready = [], False

    def add(*text):
        for line in text:
            lines.append(line)
            if echo:
                echo(line)

    def row(label, value):
        add("%-*s%s" % (L, label, value))

    boot = f["boot"]
    if not f["mount"] and not f["port"]:
        add("no board found",
            "   No CIRCUITPY drive and no serial port. Plug the board in, or pass",
            "   --mount DIR and --port TTY, or --arch NAME to build without a board.")
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
        add("   This board runs stock CircuitPython %s. Compiled modules will not"
            % (version or "?"),
            "   load. Your code still runs from /src as bytecode.",
            "   Flash turbo firmware for %s (see docs/build.md)."
            % (boot.get("board_id") or "this board"))
        return lines, False
    elif f["arch_source"] == "flag":
        row("_mpy", "not read; --arch %s given (loader presence unknown)" % f["arch"])
    elif f["mount"]:
        row("_mpy", "no serial port found; arch from board id: %s (loader presence unknown)"
            % (f["arch"] or "unknown"))

    if not f["arch"]:
        add("no arch",
            "   Board id %s is not in the table and no port answered."
            % (boot.get("board_id") or "?"),
            "   Pass --arch NAME (%s)." % ", ".join(sorted(ARCH_ID)))
        return lines, False

    path, toolchain = resolve_toolchain(version, f["abi"], mpy_cross, offline)
    add(*toolchain)
    ready = path is not None

    p = project_state(out, f["arch"], src)
    if p:
        row("project", "%s/  %d module%s, %s" % (os.path.join(out, f["arch"]), p[0],
                                                 "" if p[0] == 1 else "s",
                                                 "fresh" if not p[1] else "%d stale" % p[1]))
    if ready:
        row("ready", "turbo build compiles -march=%s" % f["arch"])
    else:
        row("not ready", "no usable mpy-cross for CircuitPython %s; pass --mpy-cross PATH"
            % (version or "?"))
    return lines, ready


def cmd_doctor(a):
    f = board_facts(a)
    lines, ready = doctor_lines(f, a.mpy_cross, a.offline, a.out,
                                echo=None if a.json else print)
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
        if a.verbose:
            for port, err in f["port_errors"]:
                print("%-*s%s%s" % (L, "", "%s: " % port if port else "", err))
    return 0 if ready else 1



# ---------------------------------------------------------------- init

def asset(*parts):
    """A bundled file (shim, examples), or None. Works from a checkout and from an
    installed wheel, where turbo_assets is package data next to this module."""
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.path.join(here, "turbo_assets"), os.path.join(here, "..")):
        path = os.path.normpath(os.path.join(base, *parts))
        if os.path.exists(path):
            return path
    return None


def cmd_init(a):
    """Create the on-board layout for this arch. Idempotent: an existing file is
    reported and left alone, never overwritten (SPEC 5.2)."""
    f = board_facts(a)
    arch = f["arch"]
    if not arch:
        doctor_lines(f, offline=True, out=a.out, src=a.src, echo=print)
        return 1

    def report(verb, path, note):
        print("%-7s%-26s%s" % (verb, path, note))

    def make_dir(path, note):
        exists = os.path.isdir(path)
        if not exists:
            os.makedirs(path)
        report("kept" if exists else "made", path.rstrip("/\\") + "/", note)

    def copy(src, dest, note, kept_note=None):
        if os.path.exists(dest):
            report("kept", dest, kept_note or note)
            return False
        d = os.path.dirname(dest)
        if d:
            os.makedirs(d, exist_ok=True)
        shutil.copyfile(src, dest)
        report("wrote", dest, note)
        return True

    shim = asset("shim", "turbo.py")
    if not shim:
        print("bundled shim not found")
        print("   turbo_assets/shim/turbo.py is missing from this install.")
        print("   Copy shim/turbo.py from the turbo repo to lib/turbo.py yourself.")
        return 1
    n = len(open(shim).read().splitlines())
    copy(shim, os.path.join("lib", "turbo.py"),
         "shim, %d lines, identity decorators on stock firmware" % n)
    make_dir(a.src, "your source, kept off sys.path so it never shadows .mpy")
    make_dir(os.path.join(a.out, arch), "where compiled modules land")

    if a.example:
        ex = asset("examples", "mandelbrot")
        if not ex:
            print("bundled example not found; see examples/mandelbrot in the repo")
            return 1
        copy(os.path.join(ex, "src", "pixels.py"), os.path.join(a.src, "pixels.py"),
             "mandelbrot, 12-bit fixed point, @turbo.viper")
        copy(os.path.join(ex, "code.py"), "code.py",
             "imports turbo, then pixels; prints the checksum",
             kept_note="not overwritten; see examples/mandelbrot/code.py")
    return 0



# ---------------------------------------------------------------- verify

# Names the viper compiler knows and CPython does not. Stubbed as int so an
# annotated signature evaluates on the host; the arithmetic is the same either way
# except for width, which is the caveat printed under -v.
VIPER_NAMES = ("ptr8", "ptr16", "ptr32", "uint", "int8", "int16", "int32", "uint8",
               "uint16", "uint32")
SEQUENCES = (bytearray, bytes, list, tuple)


class VerifyError(Exception):
    """The harness could not run. Exit 2; a difference in the answer is not this."""


class _Identity:
    """turbo and micropython, as the board sees them once the CLI has done its job:
    decorators that change nothing."""

    def __call__(self, f):
        return f

    def native(self, f):
        return f

    def viper(self, f):
        return f

    def asm_thumb(self, f):
        return f

    def const(self, x):
        return x


def load_on_host(path):
    """Exec a board module on host CPython and return its namespace. The viper type
    names and the turbo/micropython modules are stubbed for the duration of the exec
    only, so nothing leaks into the CLI's own builtins."""
    import builtins
    if not os.path.isfile(path):
        raise VerifyError("no such file: %s" % path)
    stub = _Identity()
    saved = {n: getattr(builtins, n, None) for n in VIPER_NAMES}
    injected = [n for n in VIPER_NAMES if not hasattr(builtins, n)]
    for n in VIPER_NAMES:
        setattr(builtins, n, int)
    fake = {"turbo": types.ModuleType("turbo"), "micropython": types.ModuleType("micropython")}
    fake["turbo"].turbo = stub
    fake["turbo"].arch = None
    fake["turbo"].path = "/src"
    for name in ("native", "viper", "const", "asm_thumb"):
        setattr(fake["micropython"], name, getattr(stub, name))
    kept = {k: sys.modules.get(k) for k in fake}
    sys.modules.update(fake)
    ns = {"__name__": os.path.basename(path)[:-3], "__file__": path}
    try:
        with open(path) as f:
            code = compile(f.read(), path, "exec")
        exec(code, ns)
    except Exception as e:
        raise VerifyError("%s did not run on the host: %s: %s"
                          % (path, type(e).__name__, e))
    finally:
        for n in injected:
            delattr(builtins, n)
        for n, v in saved.items():
            if v is not None:
                setattr(builtins, n, v)
        for k, v in kept.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return ns


def compare_values(a, b):
    """(same, shown_a, shown_b, summary) for one pair of results. Sequences of
    numbers are compared element-wise; everything else by equality."""
    if isinstance(a, SEQUENCES) and isinstance(b, SEQUENCES) and len(a) == len(b) \
            and all(isinstance(x, int) for x in a) and all(isinstance(x, int) for x in b):
        deltas = [abs(x - y) for x, y in zip(a, b)]
        differ = sum(1 for d in deltas if d)
        shown_a, shown_b = str(sum(a)), str(sum(b))
        if not differ:
            return True, shown_a, shown_b, "identical, %s values" % thousands(len(a))
        return False, shown_a, shown_b, "%s of %s differ, max %d, mean %.2f" % (
            thousands(differ), thousands(len(a)), max(deltas), sum(deltas) / float(len(deltas)))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        if a == b:
            return True, str(a), str(b), "identical"
        d = b - a
        pct = " (%.2f%%)" % (100.0 * abs(d) / abs(a)) if a else ""
        return False, str(a), str(b), "delta %+d%s" % (d, pct) if isinstance(d, int) \
            else "delta %+g%s" % (d, pct)
    same = a == b
    return same, repr(a)[:40], repr(b)[:40], "identical" if same else "differ"


def merge_slot(acc, a, b):
    """Fold one case's pair of results into a running per-slot comparison."""
    if isinstance(a, SEQUENCES) and isinstance(b, SEQUENCES):
        acc[0].extend(a)
        acc[1].extend(b)
    else:
        acc[0].append(a)
        acc[1].append(b)
    return acc


def run_cases(ns_a, ns_b, fn_name, cases):
    """Call fn_name in both namespaces on every case. Returns (description, rows),
    where a row is (label, same, a, b, summary). Each side gets its own copy of the
    arguments, so a function that writes into a buffer is compared by that buffer."""
    for ns, which in ((ns_a, "baseline"), (ns_b, "candidate")):
        if not callable(ns.get(fn_name)):
            raise VerifyError("%s has no function %s()" % (which, fn_name))
    # Label a mutated argument with its parameter name. Read from the code object,
    # not inspect.signature: on Python 3.14 annotations are evaluated lazily, so
    # touching them here would re-raise NameError on ptr8 outside the stub window.
    code = getattr(ns_b[fn_name], "__code__", None)
    names = list(code.co_varnames[:code.co_argcount]) if code else []
    description, slots, n = None, {}, 0
    for i, case in enumerate(cases):
        if i == 0 and isinstance(case, str):
            description = case
            continue
        args = case if isinstance(case, tuple) else (case,)
        out = []
        for ns in (ns_a, ns_b):
            call_args = copy.deepcopy(args)
            try:
                ret = ns[fn_name](*call_args)
            except Exception as e:
                raise VerifyError("%s(%s) raised on the host: %s: %s"
                                  % (fn_name, "case %d" % i, type(e).__name__, e))
            out.append((ret, call_args))
        n += 1
        (ret_a, args_a), (ret_b, args_b) = out
        if ret_a is not None or ret_b is not None:
            merge_slot(slots.setdefault("return", ([], [])), ret_a, ret_b)
        for j, (before, after_a, after_b) in enumerate(zip(args, args_a, args_b)):
            if after_a != before or after_b != before:  # the function wrote into it
                label = names[j] if j < len(names) else "arg %d" % j
                merge_slot(slots.setdefault(label, ([], [])), after_a, after_b)
    if not n:
        raise VerifyError("cases() yielded nothing to run")
    rows = []
    for label, (a, b) in slots.items():
        same, sa, sb, summary = compare_values(a, b)
        rows.append((label, same, sa, sb, summary))
    return description, rows


def cmd_verify(a):
    try:
        base = load_on_host(a.baseline)
        cand = load_on_host(a.candidate)
        if a.inputs:
            harness = load_on_host(a.inputs)
            if not callable(harness.get("cases")):
                raise VerifyError("%s defines no cases()" % a.inputs)
            if not a.fn:
                raise VerifyError("--inputs needs --fn NAME: which function to call")
            description, rows = run_cases(base, cand, a.fn, harness["cases"]())
        elif callable(base.get("_turbo_bench")) and callable(cand.get("_turbo_bench")):
            description = None
            same, sa, sb, summary = compare_values(base["_turbo_bench"](),
                                                   cand["_turbo_bench"]())
            rows = [("checksum", same, sa, sb, summary)]
        else:
            raise VerifyError("no inputs: pass --inputs, or define _turbo_bench() "
                              "in both modules")
    except VerifyError as e:
        print(e)
        return 2

    if description:
        print(description)
    for label, _, sa, sb, summary in rows:
        print("  %-9s  %s -> %s        %s" % (label, sa, sb, summary))
    if all(same for _, same, _, _, _ in rows):
        print("  identical output")
    else:
        print("  fixed point moved the answer. this is the number a reviewer wants to see.")
    if a.verbose:
        if a.fn and not a.inputs:
            print("  --fn is unused without --inputs; _turbo_bench() was compared instead")
        print("  host ints are unbounded; viper ints wrap at 32 bits, so a host match is")
        print("  necessary, not sufficient. turbo bench on a board is the final word.")
    return 0



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
    d.set_defaults(run=cmd_doctor)
    i = sub.add_parser("init", help="create lib/turbo.py, src/ and lib/turbo/<arch>/")
    i.add_argument("--port")
    i.add_argument("--mount")
    i.add_argument("--board")
    i.add_argument("--arch", help="layout for this arch instead of asking the board")
    i.add_argument("--out", default="lib/turbo")
    i.add_argument("--src", default="src")
    i.add_argument("--example", action="store_true",
                   help="also write the mandelbrot src/pixels.py and code.py")
    i.set_defaults(run=cmd_init, mpy_cross=None, offline=True, json=False, verbose=False)
    b = sub.add_parser("build", help="compile the @turbo modules in SRC for this board")
    b.add_argument("src", nargs="?", default="src")
    b.add_argument("--out", default="lib/turbo")
    b.add_argument("--mpy-cross", help="use this mpy-cross instead of the cached one")
    b.add_argument("--arch", help="comma separated, or 'all'; default is the board's arch")
    b.add_argument("--port")
    b.add_argument("--mount")
    b.add_argument("--board")
    b.add_argument("--no-copy", action="store_true",
                   help="do not copy the result to the CIRCUITPY drive")
    b.add_argument("--offline", action="store_true", help="never fetch mpy-cross")
    b.add_argument("-v", "--verbose", action="store_true")
    b.set_defaults(run=cmd_build)
    n = sub.add_parser("bench")
    n.add_argument("module")
    n.add_argument("--port", required=True)
    n.add_argument("--mount", required=True)
    n.add_argument("--out", default="lib/turbo")
    n.add_argument("--trials", type=int, default=5)
    n.add_argument("--min-gain", type=float, default=1.05,
                   help="a compiled tier must beat the bytecode median by this factor (default 1.05)")
    n.add_argument("--pyboard-tools", default=os.path.expanduser("~/cp-1030/tools"))
    n.set_defaults(run=cmd_bench)
    c = sub.add_parser("check")
    c.add_argument("src")
    c.add_argument("--out", default="lib/turbo")
    c.set_defaults(run=cmd_check)
    k = sub.add_parser("pack", help="one UF2: turbo firmware + project files (or a self-extracting code.py)")
    k.add_argument("project", help="folder with code.py, lib/, src/")
    k.add_argument("--board", help="folder2uf2 board name, e.g. adafruit_metro_rp2350")
    k.add_argument("--firmware", help="turbo firmware .uf2 to combine with")
    k.add_argument("-o", "--output")
    k.add_argument("--self-extract", action="store_true",
                   help="emit a self-extracting code.py instead; works on any port, no firmware included")
    k.add_argument("--force", action="store_true", help="pack even if a compiled module is stale")
    k.add_argument("--folder2uf2", default="folder2uf2")
    k.set_defaults(run=cmd_pack)
    y = sub.add_parser("verify", help="run two versions on host CPython and compare")
    y.add_argument("baseline", help="the version you trust, e.g. the float original")
    y.add_argument("candidate", help="the rewrite, e.g. the fixed-point version")
    y.add_argument("--fn", help="function to call with each case from --inputs")
    y.add_argument("--inputs", help="a .py file defining cases(); default is "
                                    "_turbo_bench() in both modules")
    y.add_argument("-v", "--verbose", action="store_true")
    y.set_defaults(run=cmd_verify)
    z = sub.add_parser("analyze", help="static guess at which functions turbo can speed up")
    z.add_argument("src", help="a .py file or a project folder")
    z.add_argument("--arch", help="target arch, e.g. armv7emsp")
    z.add_argument("--board", help="board id, resolves to an arch")
    z.add_argument("--json", action="store_true")
    z.set_defaults(run=cmd_analyze)
    a = p.parse_args()
    sys.exit(a.run(a) or 0)


if __name__ == "__main__":
    main()
