# turbo CLI, design and implementation spec

Written 2026-09-07 for a fresh session. Self-contained: everything needed to
build the CLI shown in section 3 of the 2026-09-08 engineering brief is here,
plus where the existing code lives. Read this whole file before touching code.

## 0. Ground rules

- **Repo:** `adafruit-turbo/` in this directory is a checkout of
  `git@github.com:mikeysklar/turbo.git` (branch `main`, private). All CLI work
  goes in `adafruit-turbo/cli/` and stays on this fork. Nothing to `adafruit/`
  and nothing announced; turbo is unreleased.
- **Existing code is the starting point, not a rewrite target.**
  `adafruit-turbo/cli/turbo_cli.py` (673 lines) already implements `build`,
  `bench`, `check`, `pack`, `analyze` with a manifest and decorator rewriting.
  Keep its conventions (section 4). Add `doctor`, `init`, `verify`, `watch`,
  toolchain auto-fetch, board autodetect, and packaging.
- **No CircuitPython checkout, no toolchain build.** The compiler is the
  official `mpy-cross` binary Adafruit publishes per release (section 2.1).
  Downloaded, cached, done. `--mpy-cross PATH` overrides for dev builds.
- **No LLM anywhere.** The tool is deterministic.
- **Never touch `code.py`.** It is never compiled (it runs from source; see 2.6).
  The CLI only reads `src/`, writes `lib/turbo/...` and `lib/turbo.py`.
- **Failures are sentences, not symptoms.** Every failure the tool can detect
  prints the exact wording in section 6, with the fix on the next line.
- **Output formats in section 5 are the spec.** Column widths, wording and
  order match; they were reviewed by the engineering leads.
- **Dependencies:** Python >= 3.9, `pyserial`. Nothing else at runtime.
  No `click`, no `rich`, no `watchdog` (poll for `watch`).
- Style: match the existing file (argparse, `%`-formatting, functions named
  `cmd_<verb>`, no classes unless there is state to hold). 99-char lines.

## 1. What the tool is for

CircuitPython carries MicroPython's native/viper machine-code emitter but ships
with it off. Turbo compiles a marked function on the host with `mpy-cross
-march=<arch>` and the board loads the resulting `.mpy` through the native
loader (`CIRCUITPY_LOAD_NATIVE=1` firmware, 2 to 3 KB over stock). Speed on
integer loops over buffers: 19.5x to 71.7x over the float bytecode people write
first, measured on 12 boards (section 2.7).

Today a person has to know four things: which `-march` their chip is, that a
`.py` beside a `.mpy` silently wins, which `mpy-cross` matches their firmware,
and where `folder2uf2` comes from. The CLI removes the first three.

## 2. Verified facts (do not re-derive; cite these)

### 2.1 Official `mpy-cross` binaries

Published by adafruit/circuitpython CI on every release
(`.github/workflows/build-mpy-cross.yml`, `build.yml` job `mpy-cross-mac`).
Public S3, no auth. Confirmed HTTP 200 on 2026-09-07 for 9.2.8, 10.0.0, 10.2.0,
10.3.0.

Base: `https://adafruit-circuit-python.s3.amazonaws.com/bin/mpy-cross/`

| Host (platform.system / machine) | Path under base |
|---|---|
| Darwin / arm64 | `macos/mpy-cross-macos-<VER>-arm64` |
| Linux / x86_64 | `linux-amd64/mpy-cross-linux-amd64-<VER>.static` |
| Linux / aarch64 | `linux-aarch64/mpy-cross-linux-aarch64-<VER>.static-aarch64` |
| Linux / armv7l (Raspbian) | `linux-raspbian/mpy-cross-linux-raspbian-<VER>.static-raspbian` (pattern from the workflow, not fetched) |
| Windows / AMD64 | `windows/mpy-cross-windows-<VER>.static.exe` |
| Darwin / x86_64 | **not published.** Failure sentence in section 6. |

`<VER>` is the exact CircuitPython version string from the board, e.g.
`10.3.0`. Beta/RC/dev builds are not published; see section 6.

The binary is standalone. macOS: Mach-O arm64, 484,528 bytes, `otool -L` shows
only `/usr/lib/libSystem.B.dylib`. Linux/Windows are static. `--version` prints:

```
CircuitPython 10.3.0 on 2026-08-31; mpy-cross emitting mpy v6.3
```

One binary emits every arch via `-march=`. `mpy-cross/mpconfigport.h` enables
all emitters (x64, x86, thumb, arm, xtensa, xtensawin, rv32). The turbo fork
never touches `mpy-cross/`; the official 10.3.0 binary produced byte-identical
output to the fork build for armv6m (604 B), armv7emsp (552 B), xtensawin
(639 B), rv32imc (606 B) on the mandelbrot module. Each compile ~2.4 ms.

Compatibility rule: the `.mpy` format is `v6.3` for all of CircuitPython 10.x.
Use the exact board version for the download; if that URL 404s, the failure
sentence applies. Do not silently substitute another version.

Amended 2026-09-08 after the farm pass. Every turbo firmware is a build after a
release tag, so every board reports `10.3.0-48-g799278aeb8` and the exact-version
rule made the fetch unreachable on the boards turbo exists for. A version of the
form `N.N.N-<count>-g<hash>[-dirty]` now falls back to its base tag, which is
published, and says so on its own line before the toolchain row. Nothing is
silent, and the binary's `mpy v<version>.<sub>` is still checked against the
board's own `_mpy` before anything is compiled. A prerelease (`10.4.0-beta.1`)
still gets the failure sentence: it sits before its tag, which may not exist.

### 2.2 Arch names and `-march` values

| `_mpy >> 10` | `-march` | Boards |
|---|---|---|
| 0 | none | stock firmware, no native loader |
| 4 | `armv6m` | Cortex-M0+: Metro M0 Express, Metro RP2040, SAMD21 |
| 5 | `armv7m` | Cortex-M3 |
| 6 | `armv7em` | Cortex-M4 no FPU |
| 7 | `armv7emsp` | Cortex-M4F / M33: Metro M4 AirLift, Metro RP2350, Feather nRF52840, Feather STM32F405, nRF54L15/LM20 |
| 8 | `armv7emdp` | Cortex-M7 / M85: EK-RA8D1 |
| 9 | `xtensa` | ESP8266 (not a CircuitPython target) |
| 10 | `xtensawin` | Xtensa LX7: Metro ESP32-S2, Metro ESP32-S3, Matrix Portal S3 |
| 11 | `rv32imc` | RISC-V: ESP32-C3, C5, C6 |

Same table as `shim/turbo.py` `_ARCH` and `cli/turbo_cli.py` `ARCH_ID`. Keep
one copy in the CLI and import it everywhere.

### 2.3 `sys.implementation._mpy` decoding

From `py/persistentcode.h:93` and `py/modsys.c:106`:

```
_mpy = MPY_VERSION | ((MPY_SUB_VERSION | (arch << 2)) << 8) | rv32_ext_flags << 16
version = _mpy & 0xff          # 6
sub     = (_mpy >> 8) & 3      # 3
arch    = (_mpy >> 10) & 0x3f  # table in 2.2
```

Values on CircuitPython 10.3.0: stock `0x0306`, armv6m `0x1306`, armv7emsp
`0x1f06`, xtensawin `0x2b06`, rv32imc `0x2f06`. **`arch == 0` means the
firmware has no native loader.** That is the capability probe; nothing else
reports it.

### 2.4 `boot_out.txt`

Written by `main.c:880` at boot to the root of CIRCUITPY:

```
Adafruit CircuitPython 10.3.0 on 2026-08-31; Metro ESP32-S3 with ESP32S3
Board ID:adafruit_metro_esp32s3
UID:0123456789ABCDEF
```

Line 1 is `"Adafruit CircuitPython " GIT_TAG " on " BUILD_DATE "; " MACHINE`
(`py/makeversionhdr.py:62`). Parse like circup (`backends.py:257`): version is
`line1.split(";")[0].split(" ")[-3]`; board id is `line2[9:]` when it starts
with `Board ID:`. Anything after boot.py output is appended; only the first two
lines matter. There is no arch in this file.

### 2.5 Default `sys.path` and import precedence

`main.c:199-204`: `["", "/", ".frozen", "/lib"]`.
`py/builtinimport.c:80` (`stat_file_py_or_mpy`): `.py` is tried before `.mpy`
in the same directory, so source beside a compiled file silently wins.
`py/builtinimport.c:108` (CIRCUITPY-CHANGE): a directory `lib/turbo/` without
`__init__` loses to the sibling file `lib/turbo.py`, which is why the shim and
the arch directory can share the name.

### 2.6 Boot order

`boot.py`, then the first existing of `code.txt`, `code.py`, `main.py`,
`main.txt` (`main.c:460`), then `repl.py` before the prompt. `code.py` always
runs from source via `pyexec_file`; compiled code only enters via `import`.

### 2.7 Measured speedups (for `analyze` and doc strings)

Mandelbrot 160x120, 64 iterations, median of 8, viper over float bytecode:
M0 Express 71.7x, RP2040 36.3x, M4 AirLift 26.0x, nRF52840 26.6x, ESP32-S2
36.1x, RP2350 24.4x, ESP32-S3 26.2x, STM32F405 19.5x, ESP32-C5 44.0x, nRF54L15
29.3x, nRF54LM20 29.3x, EK-RA8D1 38.3x. Full table:
`adafruit-turbo/docs/turbo-on-the-farm.md`. `MEASURED` in the CLI holds the
viper-over-int-bytecode numbers used by `analyze`; extend it from that doc,
never interpolate between archs.

### 2.8 The compiler's own error text

A float in a viper function:

```
Traceback (most recent call last):
  File "/tmp/f.py", line 2, in blend
ViperTypeError: can't do binary op between 'int' and 'object'
```

`mpy-cross` exits 1 and prints this to stderr. The `File ... line N` line gives
the line; `build` reports it and appends the hint from section 6.

## 3. On-board layout and the shim (existing, keep)

```
CIRCUITPY/
  code.py                       user's program, never touched
  lib/turbo.py                  shim, copied from adafruit-turbo/shim/turbo.py
  lib/turbo/turbo.json          manifest (section 4.3)
  lib/turbo/<arch>/<mod>.mpy    installed compiled module for this arch
  lib/turbo/<arch>/<mod>.viper.mpy   candidates kept beside it
  lib/turbo/<arch>/<mod>.native.mpy
  src/<mod>.py                  source, off sys.path, the fallback
```

`shim/turbo.py` (40 lines, pure Python, no changes needed): at import it reads
`_mpy >> 10`, maps to the arch name, and if `/lib/turbo/<arch>` exists inserts
it and then `/src` at the front of `sys.path` (`/src` always; the arch dir only
if present). It exposes `turbo.arch`, `turbo.path`, and identity decorators
`@turbo`, `@turbo.native`, `@turbo.viper`. On stock firmware everything runs
from `/src` as bytecode with identical results.

User code contract: `code.py` must `import turbo` before importing any
accelerated module. Modules opt in with `from turbo import turbo` and one of
the three decorators.

## 4. Existing CLI conventions (keep exactly)

### 4.1 Decorator rewrite (`rewrite(src_text, tier)`)

`DECO = ^(\s*)@turbo(\.native|\.viper)?\s*$`. Tier `viper`: `@turbo.viper` ->
`@micropython.viper`, every other `@turbo*` -> `@micropython.native`. Tier
`native`: every `@turbo*` -> `@micropython.native`. Returns `None` when the
module has no `@turbo` lines (module skipped). The `from turbo import turbo`
line is left in place; it resolves on the board because `turbo` is already in
`sys.modules`.

### 4.2 Compile (`compile_variant(mpy_cross, text, name, arch, dest)`)

Writes the rewritten text to a temp `<name>.py` (the module name must match the
file name; `mpy-cross` embeds it), runs
`mpy-cross -march=<arch> <src> -o <dest>`, returns the last stderr line on
failure. Extend it to return the full stderr so `build` can show the `File`
line too.

### 4.3 Manifest `lib/turbo/turbo.json`

```json
{
  "pixels": {
    "src": "src/pixels.py",
    "sha256": "<sha256 of src when every arch built>",
    "armv7emsp": {
      "candidates": {"viper": 552, "native": 1204},
      "installed": "viper",
      "measured": false,
      "mpy_abi": "6.3",
      "bench": {...}, "speedup_vs_bytecode": 17.4
    }
  }
}
```

Rules already in `cmd_build`: `sha256` is written only when every arch built,
so `check` reports STALE after any failure; a rebuild clears `measured`,
`bench`, `speedup_vs_bytecode`; if nothing compiled, the installed `.mpy` is
removed so an old binary never stands in for new source.

### 4.4 `_turbo_bench()` contract

A module may define `_turbo_bench()` returning a comparable value. `bench`
times it per variant and rejects any variant whose value differs from the
bytecode run. `verify` (new) reuses this contract on the host (section 5.5).

### 4.5 Existing commands, unchanged

- `build SRC [--out lib/turbo] [--mpy-cross PATH] [--arch a,b]`
- `bench MODULE --port TTY --mount DIR [--trials N] [--min-gain 1.05]`
  (uses `pyboard.py` from `--pyboard-tools`; replace with the raw-REPL helper
  in section 7 so no CircuitPython checkout is needed)
- `check SRC [--out lib/turbo]`
- `pack PROJECT --board B --firmware FW.uf2 [-o out.uf2] | --self-extract`
- `analyze SRC [--arch A | --board B] [--json]`

## 5. Commands, with exact output

Global flags: `--port TTY`, `--mount DIR`, `--board ID`, `--arch NAME`,
`--mpy-cross PATH`, `--offline`, `-v`. Autodetect fills the first three
(section 7). `--arch` is only needed with no board attached.

### 5.1 `turbo doctor`

Prints the three facts and the toolchain state; fetches `mpy-cross` if
missing (skip with `--offline`, print `not cached` and the fetch line as a
hint). Exit 0 when a build could run, 1 otherwise. `--json` emits the same as a
dict.

```
$ turbo doctor
board       Metro ESP32-S3                adafruit_metro_esp32s3
port        /dev/cu.usbmodem14201
drive       /Volumes/CIRCUITPY
firmware    CircuitPython 10.3.0
_mpy        0x2b06   arch xtensawin · mpy 6.3 · native loader present
toolchain   fetching mpy-cross  macos-arm64  10.3.0
            https://adafruit-circuit-python.s3.amazonaws.com/bin/mpy-cross/macos/mpy-cross-macos-10.3.0-arm64
            cached  ~/.cache/turbo/mpy-cross/10.3.0/macos-arm64/mpy-cross   484 KB
ready       turbo build compiles -march=xtensawin
```

Second run replaces the three `toolchain` lines with
`toolchain   ~/.cache/turbo/mpy-cross/10.3.0/macos-arm64/mpy-cross   484 KB   mpy v6.3`.
With a project present (a `src/` dir), add
`project     lib/turbo/xtensawin/  2 modules, fresh` or `... 1 stale` from the
manifest. Column: label padded to 12, then value.

Sources: `board`/`firmware` from `boot_out.txt` on the mount; `_mpy` from the
REPL (section 7.3). If there is a mount but no port, print `_mpy        no
serial port found; arch from board id: xtensawin (loader presence unknown)`
using `BOARD_ARCH`. If neither, print the section 6 sentence and exit 1.
Human-readable board name comes from line 1 of `boot_out.txt` after the `;`
and before ` with `.

### 5.2 `turbo init`

Creates the layout for the detected (or `--arch`) arch. Idempotent; never
overwrites an existing file. Copies the shim from the package's bundled copy
(ship `shim/turbo.py` inside the wheel as package data).

```
$ turbo init
wrote  lib/turbo.py              shim, 40 lines, identity decorators on stock firmware
made   src/                      your source, kept off sys.path so it never shadows .mpy
made   lib/turbo/xtensawin/      where compiled modules land
```

Existing items print `kept` instead of `wrote`/`made`. With `--example`, also
write `src/pixels.py` and `code.py` from `examples/mandelbrot/` only if
`code.py` does not exist (otherwise print
`kept   code.py                  not overwritten; see examples/mandelbrot/code.py`).

### 5.3 `turbo build [SRC=src]`

Default arch: the connected board's arch from the probe; with no board, `--arch`
is required (sentence in section 6). `--arch all` builds every entry in the 2.2
table with a `-march`. Compiles both tiers per arch, installs viper if it
compiled else native (existing rule), updates the manifest, copies to the mount
when one is present (skip with `--no-copy`).

Amended 2026-09-08 after the farm pass. Copying only `lib/turbo/<arch>/` left a
board that could not import anything: the shim was never installed, so the arch
directory never reached `sys.path`, and the acceptance run in section 10 needed
three files copied by hand. `build` now writes the installed `.mpy` files, the
manifest, `lib/turbo.py` and the `src/<mod>.py` fallback for each module it
built. `code.py` is still never touched. Files whose bytes already match are
skipped, because every write to a CIRCUITPY drive costs an autoreload, and the
summary names only what actually changed.

```
$ turbo build src/
pixels     viper   xtensawin    639 B      native   1,204 B
blend      FAILED  src/blend.py:4
           ViperTypeError: can't do binary op between 'int' and 'object'
           a float reached a viper function; scale to integers
1 built, 1 failed, 5 ms
```

Per-module line: name padded to 10; tier, arch padded to 10, size with
thousands separator and ` B`; second tier after 6 spaces. On failure: `FAILED`
then `path:line` from the compiler's `File` line; the compiler's last stderr
line; then the hint from section 6 keyed on the error text. If viper failed but
native compiled, print the native size on the next line as
`           native  xtensawin  1,204 B   installed` (the module still ships,
just slower). Exit 1 if any module has no installable variant.

### 5.4 `turbo analyze [SRC=src]` (existing, two additions)

Keep the existing output. Add the line number to float findings:
`loop x1, float multiply at line 4`. Default `--arch` from the probe when a
board is attached. Everything else per `docs/analyze.md`.

### 5.5 `turbo verify BASELINE.py CANDIDATE.py --fn NAME`

New. Runs both files on **host CPython** and compares. Purpose: show how far a
fixed-point rewrite moved the answer. No board, no `mpy-cross`.

```
$ turbo verify src/mandel_float.py src/pixels.py --fn mandel_row --inputs cases.py
120 rows x 160 px, 64 iterations, both versions run on the host
  out        407790 -> 407644        335 of 19,200 differ, max 42, mean 0.09
  fixed point moved the answer. this is the number a reviewer wants to see.
```

Measured on the host 2026-09-07, not invented: the float loop checksums 407790,
the 12-bit fixed-point loop 407644, and 335 of the 19,200 pixels land on a
different iteration count. Without `--inputs` the same pair is compared through
`_turbo_bench()` and prints one `checksum` row. The earlier draft of this spec
said `581 -> 576`; those numbers were never run.

Mechanics:

- Exec each file in a fresh namespace with viper names stubbed:
  `ptr8 = ptr16 = ptr32 = uint = int`, and a fake `turbo` module whose
  decorators are identity (so `from turbo import turbo` and `@turbo.viper`
  work under CPython). Inject the stubs into `builtins` for the exec only.
- Inputs come from a harness, in this order of preference: `--inputs FILE.py`
  defining `cases()` that yields argument tuples for `--fn`; else the module's
  `_turbo_bench()` (call it in both, compare return values); else fail with
  `no inputs: pass --inputs, or define _turbo_bench() in both modules`.
- If `--fn` is given with `cases()`, call `fn(*args)` in both, and for each
  case compare: scalars by equality (report delta if numeric); buffers
  (`bytearray`, `list`, `array`) element-wise (report count differing, max and
  mean abs delta). Print the harness's own summary line first if `cases()`
  returns a description string as its first yield.
- The example output above is what the mandelbrot harness prints; the general
  format is `  <label>   <baseline> -> <candidate>        <delta summary>` then
  one sentence: `identical output` or
  `fixed point moved the answer. this is the number a reviewer wants to see.`
- Caveat printed once in `-v`: host ints are unbounded; viper ints wrap at 32
  bits, so a host match is necessary, not sufficient. `bench` on a board is
  the final word.
- Exit 0 whether or not they differ; exit 2 on harness errors. Differing is
  information, not failure.

Ship `examples/mandelbrot/src/mandel_float.py` (the float version of the loop,
from `docs/turbo-on-the-farm.md` "float bytecode" variant) and a `cases()`
harness so the documented command works out of the box.

### 5.6 `turbo watch [SRC=src]`

Poll `src/*.py` mtimes every 0.5 s (no inotify dependency). On change: rebuild
that module only (same rules as `build`), copy the installed `.mpy` to the
mount's `lib/turbo/<arch>/` and the source to `src/`, print one line. The board
autoreloads on the file write; the CLI does not need to trigger it.

```
$ turbo watch src/
watching src/  ->  /Volumes/CIRCUITPY/lib/turbo/xtensawin/
12:41:07  pixels.py changed   rebuilt 2 variants   copied   board reloaded
12:41:32  blend.py changed    FAILED src/blend.py:4   ViperTypeError: ...   not copied
```

`board reloaded` is printed only when a port is open and the CLI sees the
`soft reboot` banner within 3 s; otherwise `copied` ends the line. Ctrl-C
exits 0. Requires a mount; sentence in section 6 otherwise.

### 5.7 `turbo bench`, `check`, `pack`

Unchanged behavior. Swap `pyboard.py` for the raw-REPL helper (7.3) and let
`--port`/`--mount` autodetect.

## 6. Failure sentences (verbatim)

Print the bold line, then the explanation and fix indented three spaces.

```
_mpy 0x0306   arch 0, no native loader
   This board runs stock CircuitPython 10.3.0. Compiled modules will not
   load. Your code still runs from /src as bytecode.
   Flash turbo firmware for adafruit_metro_esp32s3 (see docs/build.md).

lib/turbo/armv7emsp/   present, but this board is xtensawin
   Built for a different board.   turbo build --arch xtensawin

src/pixels.py   newer than lib/turbo/xtensawin/pixels.viper.mpy
   Stale.   turbo build

firmware 10.4.0-beta.1   no published mpy-cross for that version
   Adafruit publishes mpy-cross per release only. Use a release build,
   or point turbo at a local mpy-cross with --mpy-cross PATH.

host macOS x86_64   Adafruit builds macOS arm64 only
   Rosetta (arch -arm64 is not available on Intel), or a local build:
   make -C mpy-cross in a CircuitPython checkout, then --mpy-cross PATH.

mpy-cross 9.2.8 cached, board runs 10.3.0
   Different .mpy format. Fetching 10.3.0.   (then fetch; no flag needed)

no board found
   No CIRCUITPY drive and no serial port. Plug the board in, or pass
   --mount DIR and --port TTY, or --arch NAME to build without a board.

no serial port found
   Reading version and board from /Volumes/CIRCUITPY/boot_out.txt.
   Arch guessed from board id. Loader presence unknown until a port is found.

no CIRCUITPY drive
   turbo watch and turbo build copy need a drive. Pass --mount DIR, or
   use turbo build --no-copy and copy lib/turbo/ yourself.

/media/sklarm/CIRCUITPY   Read-only file system
   CircuitPython may have the filesystem for itself (a storage.remount in
   boot.py, or safe mode), or the host remounted it after an I/O error.
   Reset or replug the board and run again, or use --no-copy and copy
   lib/turbo/ to the board yourself.

ViperTypeError: can't do binary op between 'int' and 'object'
   a float reached a viper function; scale to integers
ViperTypeError: local 'x' has type 'int' but source is 'object'
   a value came from a Python object; declare the parameter type
   (out: ptr8, n: int) or convert with int()
SyntaxError: invalid micropython decorator
   this firmware has no emitter; the decorator must go through turbo build,
   not run from source
ValueError: incompatible .mpy arch
   (seen on the board) the .mpy is for a different arch; turbo doctor
ValueError: native code in .mpy unsupported
   (seen on the board) stock firmware; see the arch 0 sentence above
```

Hint lookup for `build`: match the compiler's last stderr line by prefix
against the last five entries; print the matching hint or nothing.

## 7. Board detection and the serial probe

### 7.1 Mount

Search in order: `--mount`; `$CIRCUITPY_MOUNT`; on macOS `/Volumes/CIRCUITPY`;
on Linux `/media/*/CIRCUITPY`, `/run/media/*/CIRCUITPY`, `/mnt/CIRCUITPY`; on
Windows every drive letter whose volume label is `CIRCUITPY`. A mount counts
only if `boot_out.txt` exists on it. If several, prefer the one whose
`Board ID` matches `--board`, else the first and print `drive       ... (2
found, using the first; pass --mount)`.

### 7.2 Port

`serial.tools.list_ports.comports()`, keep entries with VID `0x239A`
(Adafruit) or any entry whose description contains `CircuitPython`. On macOS
prefer `/dev/cu.usbmodem*` over `/dev/tty.*`. If the board exposes two CDC
ports, the REPL is the first (data is the second); try each and keep the one
that answers the raw-REPL prompt. Match port to mount by the USB serial number
when available (`boot_out.txt` UID vs `comports().serial_number`); otherwise
first match.

### 7.3 Raw REPL helper (replaces `pyboard.py`)

Implement `repl.py` in the package, ~80 lines, pyserial only:

1. Open at 115200, timeout 2 s. Send `\r\x03\x03` (interrupt twice).
2. Send `\r\x01` (raw mode). Read until `raw REPL; CTRL-B to exit\r\n>`.
3. Send code, then `\x04`. Read `OK`. Read until `\x04` (stdout), then until
   `\x04` (stderr), then `>`.
4. Send `\r\x02` to leave raw mode when done. Do not soft-reset unless asked;
   the user's program was interrupted by step 1, and CircuitPython resumes it
   on the next reload or Ctrl-D.

Probe code, sent as one exec:

```python
import sys; print(sys.implementation._mpy, sys.implementation.version)
```

Parse the first int. If `AttributeError` (no `_mpy`), the firmware was built
without `MICROPY_PERSISTENT_CODE_LOAD`; report `arch 0` with the stock
sentence. Timeouts: 3 s total; on timeout print the `no serial port` sentence
and continue with mount-only facts.

Bench (existing) sends the module's `_turbo_bench()` call the same way with a
600 s timeout.

## 8. Toolchain fetch and cache

- Cache root: `$TURBO_CACHE` else `~/.cache/turbo`. Binary at
  `<root>/mpy-cross/<VER>/<os-arch>/mpy-cross[.exe]`, `chmod 755`.
- Platform key: `macos-arm64`, `linux-amd64`, `linux-aarch64`,
  `linux-raspbian` (Linux + `armv7l`), `windows`. `Darwin` + `x86_64` -> the
  section 6 sentence, exit 1 unless `--mpy-cross`.
- Fetch with `urllib.request`, 30 s timeout, to a temp file, then rename.
  Print the URL while fetching (as in 5.1). On 404 print the beta/dev sentence.
- Validate after fetch: run `<bin> --version`, require the string to start
  with `CircuitPython <VER>` and contain `mpy v<version>.<sub>` matching the
  board's `_mpy` decode. Mismatch: delete and print
  `mpy-cross <VER> reports mpy v6.2, board wants v6.3; refusing to use it`.
- `--mpy-cross PATH` bypasses fetch and validation except the `--version`
  print in `-v`.
- `--offline`: never fetch; if the binary is missing, print the fetch line as
  a hint and exit 1 from `doctor`/`build`.

## 9. Packaging

- `adafruit-turbo/pyproject.toml`: name `adafruit-turbo`, console script
  `turbo = turbo_cli.main:main`. Move `cli/turbo_cli.py` into a package
  `cli/turbo_cli/` (`__init__.py`, `main.py`, `probe.py`, `repl.py`,
  `toolchain.py`, `verify.py`, `watch.py`) only if the split is done in one
  commit with no behavior change; otherwise keep the single file and add a
  thin `main.py`. Include `shim/turbo.py` as package data.
- Install path in docs: `pipx install adafruit-turbo` (from the repo URL until
  a release exists: `pipx install git+ssh://git@github.com/mikeysklar/turbo.git#subdirectory=cli`).
- Version: `0.1.0`. `turbo --version` prints it plus the cached `mpy-cross`
  versions.

## 10. Tests and acceptance

Unit (pytest, no hardware):

- `_mpy` decode: the five values in 2.3 round-trip to (arch name, "6.3").
- `boot_out.txt` parse: the sample in 2.4, plus a file with boot.py output
  appended, plus a missing `Board ID` line.
- URL builder: the five platform keys produce the exact paths in 2.1.
- `rewrite()`: existing behavior, both tiers, no-decorator returns `None`.
- Hint lookup: each error prefix in section 6 maps to its hint.
- `verify`: the mandelbrot float vs fixed-point pair reports `407790 -> 407644`
  (the checksum of the 160x120 grid, confirmed against `_turbo_bench()` on the
  host 2026-09-07).

Integration (farm, `bravo`; see the `hil-farm` skill and
`adafruit-turbo/tools/farm/deploy-test.sh`):

- `turbo doctor` on a stock 10.3.0 board prints the arch-0 sentence and exits 1.
  Confirmed 2026-09-08: the farm Metro RP2040 flashed to stock reports
  `_mpy 0x0306`, and `build`, `watch` and `init` print the same diagnosis
  rather than claiming no board was found. `init` still lays the project out,
  because the shim and the `/src` fallback are exactly what a stock board needs;
  it skips only the arch directory. The sentence's claim was checked on that
  board: `arch=None path=/src checksum=407644` in 8330 ms, against 422 ms
  compiled.
- `turbo doctor` on each loader-only board prints the right arch: RP2040
  `armv6m`, RP2350/M4/nRF52840/STM32F405 `armv7emsp`, ESP32-S2/S3
  `xtensawin`, C5 `rv32imc`.
- `turbo build` output for `examples/mandelbrot/src/pixels.py` is
  byte-identical to a direct `mpy-cross -march=<arch>` run of the rewritten
  source (`cmp` the `.viper.mpy`).
- `turbo init && turbo build` provisions the board on its own; copy `code.py`,
  which is yours, and the example prints `checksum=407644` (the shim test's
  known-good value from `docs/shim-test.md`) with `path=/lib/turbo/<arch>`.
  Confirmed 2026-09-08 on the RP2040 (422 ms) and ESP32-S3 (186 ms).
- `turbo watch`: edit `src/pixels.py`, see the one-line report, board reloads.

## 11. Explicitly out of scope for this pass

- A flat `lib/<mod>.mpy` layout without the shim (argued for in the brief as a
  simplification; not in the reviewed CLI example, so not here).
- Per-line blockers in `analyze` beyond the line-number addition in 5.4.
- Splitting a function out of `code.py` (that is the web page's job).
- circup integration, bundle publishing, `boot_out.txt` arch line (upstream).
- Any web or wasm work; see `turbo-web.md`.

## 12. Files to read first in the fresh session

- `adafruit-turbo/cli/turbo_cli.py` (all of it)
- `adafruit-turbo/shim/turbo.py`
- `adafruit-turbo/examples/mandelbrot/{code.py,src/pixels.py}`
- `adafruit-turbo/docs/analyze.md`, `docs/shim-test.md`, `docs/turbo-on-the-farm.md`
- `adafruit-turbo/tools/farm/deploy-test.sh` (how the farm copies and runs)
- `/Volumes/clear/Downloads/CLAUDE.md` (working rules: plan mode, one edit per
  turn, approval before edits)
