# turbo CLI, design and implementation spec

Drafted 2026-09-07, implemented and verified on hardware 2026-09-08. This
describes the tool as it now behaves, not as it was first imagined; section 13
records where the two differed and why. Every number here was measured. Read
this whole file before changing code.

## 0. Ground rules

- **Repo:** `mikeysklar/turbo-cli`, private. The firmware, docs and examples
  live in `mikeysklar/turbo`; only the host CLI lives here. Nothing goes to
  `adafruit/` and nothing is announced. Turbo is unreleased.
- **No CircuitPython checkout, no toolchain build.** The compiler is the
  official `mpy-cross` binary Adafruit publishes per release (section 2.1),
  downloaded and cached. `--mpy-cross PATH` overrides it.
- **No LLM anywhere.** The tool is deterministic.
- **Never touch `code.py`.** It is never compiled; it runs from source (2.6).
  The CLI reads `src/` and writes `lib/turbo.py`, `lib/turbo/...` and `src/`
  on the board.
- **Failures are sentences, not symptoms.** Every failure the tool can detect
  prints the wording in section 6, with the fix indented under it. A traceback
  reaching the user is a bug.
- **Output formats in section 5 are the spec.** Column widths, wording and
  order match.
- **Dependencies:** Python >= 3.9 and `pyserial`. Nothing else at runtime. No
  `click`, no `rich`, no `watchdog`; `watch` polls.
- Style: argparse, `%`-formatting, functions named `cmd_<verb>`, no classes
  unless there is state to hold, 99-char lines.

## 1. What the tool is for

CircuitPython carries MicroPython's native/viper machine-code emitter but ships
with it off. Turbo compiles a marked function on the host with `mpy-cross
-march=<arch>` and the board loads the resulting `.mpy` through the native
loader (`CIRCUITPY_LOAD_NATIVE=1` firmware, 2 to 3 KB over stock). Speed on
integer loops over buffers: 19.5x to 71.7x over the float bytecode people write
first, measured on 12 boards (section 2.7).

Without the CLI a person has to know four things: which `-march` their chip is,
that a `.py` beside a `.mpy` silently wins, which `mpy-cross` matches their
firmware, and where `folder2uf2` comes from. The CLI removes the first three.

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

Two rows fetched and run for real: macOS arm64 10.3.0 is 484,528 bytes, Linux
amd64 10.3.0 is 1,411,184 bytes. Both report

```
CircuitPython 10.3.0 on 2026-08-31; mpy-cross emitting mpy v6.3
```

The binary is standalone. macOS is Mach-O arm64 and `otool -L` shows only
`/usr/lib/libSystem.B.dylib`; Linux and Windows are static. One binary emits
every arch via `-march=`: `mpy-cross/mpconfigport.h` enables all emitters (x64,
x86, thumb, arm, xtensa, xtensawin, rv32). The turbo fork never touches
`mpy-cross/`.

**Which version to fetch.** The `.mpy` format is `v6.3` for all of CircuitPython
10.x. Fetch the board's exact version when it is a release, `N.N.N`. When it is
a build after a release tag, `N.N.N-<count>-g<hash>` or the same with `-dirty`,
fetch the base tag instead and say so on its own line before the toolchain row.
That case is not an edge: every turbo firmware is such a build, so treating it
as unfetchable would make the download unreachable on exactly the boards turbo
exists for. The substitution is never silent, and the binary's reported
`mpy v<version>.<sub>` is still checked against the board's own `_mpy` before
anything is compiled. A prerelease, `10.4.0-beta.1` or `10.4.0-rc.2`, gets the
failure sentence instead: it sits *before* its tag, which may not exist yet.

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

The same table appears in `shim/turbo.py` as `_ARCH` and in the CLI as
`ARCH_ID`. A unit test asserts the two agree.

### 2.3 `sys.implementation._mpy` decoding

From `py/persistentcode.h:93` and `py/modsys.c:106`:

```
_mpy = MPY_VERSION | ((MPY_SUB_VERSION | (arch << 2)) << 8) | rv32_ext_flags << 16
version = _mpy & 0xff          # 6
sub     = (_mpy >> 8) & 3      # 3
arch    = (_mpy >> 10) & 0x3f  # table in 2.2
```

Values on CircuitPython 10.3.0: stock `0x0306`, armv6m `0x1306`, armv7emsp
`0x1f06`, xtensawin `0x2b06`, rv32imc `0x2f06`. All five read back from farm
hardware. **`arch == 0` means the firmware has no native loader.** That is the
capability probe; nothing else reports it.

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
with `Board ID:`. Anything after boot.py output is appended; only the first
three lines matter. There is no arch in this file.

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
`turbo/docs/turbo-on-the-farm.md`. `MEASURED` in the CLI holds the
viper-over-int-bytecode numbers used by `analyze`; extend it from that doc,
never interpolate between arches.

### 2.8 The compiler's own error text

A float in a viper function:

```
Traceback (most recent call last):
  File "pixels.py", line 5, in blend
ViperTypeError: can't do binary op between 'int' and 'object'
```

`mpy-cross` exits 1 and prints this to stderr. The `File ... line N` line gives
the line; the rewritten source has the same line numbering as the original, so
that number is the user's. `build` reports it as `src/<file>.py:<line>` and
appends the hint from section 6.

## 3. On-board layout and the shim

```
CIRCUITPY/
  code.py                       user's program, never touched
  lib/turbo.py                  shim
  lib/turbo/turbo.json          manifest (section 4.3)
  lib/turbo/<arch>/<mod>.mpy    installed compiled module for this arch
  src/<mod>.py                  source, off sys.path, the fallback
```

`build` writes everything above except `code.py`. The `<mod>.viper.mpy` and
`<mod>.native.mpy` candidates stay in the host project and are not copied.

`shim/turbo.py` (49 lines, pure Python): at import it reads `_mpy >> 10`, maps
to the arch name, and if `/lib/turbo/<arch>` exists inserts it and then `/src`
at the front of `sys.path` (`/src` always; the arch dir only if present). It
exposes `turbo.arch`, `turbo.path`, and identity decorators `@turbo`,
`@turbo.native`, `@turbo.viper`. On stock firmware everything runs from `/src`
as bytecode with identical results, confirmed on hardware: a stock Metro RP2040
printed `arch=None path=/src checksum=407644` in 8330 ms against 422 ms for the
same module compiled.

User code contract: `code.py` must `import turbo` before importing any
accelerated module. Modules opt in with `from turbo import turbo` and one of
the three decorators.

## 4. Conventions

### 4.1 Decorator rewrite (`rewrite(src_text, tier)`)

`DECO = ^(\s*)@turbo(\.native|\.viper)?\s*$`. Tier `viper`: `@turbo.viper` ->
`@micropython.viper`, every other `@turbo*` -> `@micropython.native`. Tier
`native`: every `@turbo*` -> `@micropython.native`. Returns `None` when the
module has no `@turbo` lines, and the module is skipped. The
`from turbo import turbo` line is left in place; it resolves on the board
because `turbo` is already in `sys.modules`. One decorator line becomes one
line, so line numbers are preserved.

### 4.2 Compile (`compile_variant(mpy_cross, text, name, arch, dest)`)

Writes the rewritten text to a temp `<name>.py`, since the module name must
match the file name and `mpy-cross` embeds it. Returns `None` on success, the
whole stderr on failure, so `build` can show the `File` line as well as the
message.

**`mpy-cross` is run from inside the temp directory with a bare basename, and
`dest` is made absolute first.** It embeds the source path exactly as given on
the command line. Passing an absolute temp path put `/tmp/tmpXXXX/` inside
every binary: two builds of identical source produced different bytes, and a
traceback on the board named a directory that never existed on the user's
machine. With a bare basename the embedded name is `pixels.py`, builds are
reproducible, and section 10's byte-identical test passes.

The embedded name is part of the file, so its length shifts the size. The
mandelbrot module compiles to 603 B armv6m, 551 B armv7m/armv7em/armv7emsp/
armv7emdp, 657 B xtensa, 638 B xtensawin, 605 B rv32imc.

### 4.3 Manifest `lib/turbo/turbo.json`

```json
{
  "pixels": {
    "src": "src/pixels.py",
    "sha256": "<sha256 of src when every arch built>",
    "armv7emsp": {
      "candidates": {"viper": 551, "native": 599},
      "installed": "viper",
      "measured": false,
      "mpy_abi": "6.3",
      "bench": {}, "speedup_vs_bytecode": 17.4
    }
  }
}
```

`sha256` is written only when every arch built, so `check` reports STALE after
any failure. A rebuild clears `measured`, `bench` and `speedup_vs_bytecode`. If
nothing compiled, the installed `.mpy` is removed, so an old binary never
stands in for new source.

### 4.4 `_turbo_bench()` contract

A module may define `_turbo_bench()` returning a comparable value. `bench`
times it per variant and rejects any variant whose value differs from the
bytecode run. `verify` reuses the same contract on the host (section 5.5).

## 5. Commands, with exact output

Flags shared by the board-aware commands: `--port TTY`, `--mount DIR`,
`--board ID`, `--arch NAME`, `--mpy-cross PATH`, `--offline`, `-v`. Autodetect
fills the first three (section 7). `--arch` is only needed with no board
attached.

### 5.1 `turbo doctor`

Prints the board facts and the toolchain state, fetching `mpy-cross` if it is
missing. Exit 0 when a build could run, 1 otherwise. `--json` emits the same as
a dict.

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

Label padded to 12, then the value. The board name is padded to 28 with two
spaces after it, so a long name such as `Adafruit Feather nRF52840 Express`
still leaves a gap before the board id. Paths under `$HOME` print with `~`.

A second run replaces the three `toolchain` lines with one:
`toolchain   ~/.cache/turbo/mpy-cross/10.3.0/macos-arm64/mpy-cross   484 KB   mpy v6.3`.
On turbo firmware, which is always a build after a release tag, a line above it
names the substitution from 2.1:

```
firmware    CircuitPython 10.3.0-42-g3cdb20693f
_mpy        0x1306   arch armv6m · mpy 6.3 · native loader present
            dev build of 10.3.0; using the 10.3.0 mpy-cross, abi checked below
toolchain   ~/.cache/turbo/mpy-cross/10.3.0/linux-amd64/mpy-cross   1411 KB   mpy v6.3
ready       turbo build compiles -march=armv6m
```

With a project present (a `src/` dir), add
`project     lib/turbo/xtensawin/  2 modules, fresh` or `... 1 stale` from the
manifest.

Sources: `board` and `firmware` from `boot_out.txt` on the mount, `_mpy` from
the REPL (7.3). The human-readable board name is line 1 of `boot_out.txt` after
the `;` and before ` with `. When there is a mount but no port, the `_mpy` row
reads `no serial port found; arch from board id: xtensawin (loader presence
unknown)` from `BOARD_ARCH`. When a port was found but would not open, say that
instead: it is a different problem with a different fix, and it is the most
common one, since any serial monitor or browser page holds the port. With
neither mount nor port, print the section 6 sentence and exit 1.

### 5.2 `turbo init`

Creates the project layout. Idempotent: an existing file is reported and left
alone, never overwritten. The shim comes from the project's own `lib/turbo.py`
if it has one, else the copy bundled in the wheel.

```
$ turbo init
wrote  lib/turbo.py              shim, 49 lines, identity decorators on stock firmware
made   src/                      your source, kept off sys.path so it never shadows .mpy
made   lib/turbo/xtensawin/      where compiled modules land
```

Verb padded to 7, path to 26. Existing items print `kept`. With `--example`,
also write `src/pixels.py` and `code.py` from the bundled mandelbrot example,
leaving an existing `code.py` alone with
`kept   code.py                   not overwritten; see examples/mandelbrot/code.py`.

**With no arch, init still lays the project out** and skips only the arch
directory, printing the reason first and then

```
skip   lib/turbo/<arch>/         no arch yet; run again with a turbo board, or --arch NAME
```

Exit 0. A stock board is precisely the case the identity decorators exist for;
refusing there would leave that user with nothing.

### 5.3 `turbo build [SRC=src]`

Default arch is the connected board's, from the probe. `--arch` takes a comma
separated list, or `all` for every arch in 2.2. Compiles both tiers per arch,
installs viper if it compiled else native, updates the manifest, and copies to
the mount unless `--no-copy`.

```
$ turbo build
pixels     viper   armv6m       603 B      native     635 B
1 built, 1016 ms, copied 1 module, shim to /media/sklarm/CIRCUITPY5
```

Per-module line: name padded to 11, tier to 8, arch to 10, size right-aligned
in 6 with thousands separators and ` B`; the other tier after six spaces. On
failure:

```
blend      FAILED  src/blend.py:5
           ViperTypeError: can't do binary op between 'int' and 'object'
           a float reached a viper function; scale to integers
           native  armv6m       467 B   installed
```

`FAILED` then `path:line` from the compiler's `File` line, the compiler's last
stderr line, then the hint from section 6 keyed on it. If viper failed but
native compiled, the module still ships and the last line says so. Exit 1 if
any module has no installable variant.

**What is copied.** The installed `.mpy` files, the manifest, `lib/turbo.py`
and the `src/<mod>.py` fallback for each module built. Not `code.py`. Copying
only the arch directory leaves a board that cannot import anything, because the
shim is what puts that directory on `sys.path`. Files whose bytes already match
are skipped, since every write to a CIRCUITPY drive costs the board an
autoreload; the summary names only what changed, or says
`/media/sklarm/CIRCUITPY5 already up to date`.

**When the drive refuses a write**, print the section 6 sentence, end the
summary with `built but not copied`, and exit 1. The modules compiled; only the
copy failed, and a successful compile must not end in a traceback.

**On a board with no native loader**, print doctor's diagnosis, which knows the
difference between a stock board and no board at all, and exit 1.

### 5.4 `turbo analyze [SRC=src]`

Static triage, unchanged from `docs/analyze.md`: grades each function by shape
and says whether turbo is worth the trouble. Default `--arch` from the probe
when a board is attached.

### 5.5 `turbo verify BASELINE.py CANDIDATE.py --fn NAME`

Runs both files on **host CPython** and compares, to show how far a fixed-point
rewrite moved the answer. No board, no `mpy-cross`.

```
$ turbo verify src/mandel_float.py src/pixels.py --fn mandel_row --inputs cases.py
120 rows x 160 px, 64 iterations, both versions run on the host
  out        407790 -> 407644        335 of 19,200 differ, max 42, mean 0.09
  fixed point moved the answer. this is the number a reviewer wants to see.
```

Measured, not invented: the float loop checksums 407790, the 12-bit fixed-point
loop 407644, and 335 of the 19,200 pixels land on a different iteration count.
Without `--inputs` the same pair is compared through `_turbo_bench()` and prints
one `checksum` row.

Mechanics:

- Exec each file in a fresh namespace with the viper type names stubbed to
  `int`, and fake `turbo` and `micropython` modules whose decorators are
  identity, so `from turbo import turbo` and `@turbo.viper` work under CPython.
  The stubs go into `builtins` and `sys.modules` for the exec only and are
  removed afterwards.
- Inputs, in order of preference: `--inputs FILE.py` defining `cases()` that
  yields argument tuples for `--fn`; else `_turbo_bench()` in both modules;
  else fail with `no inputs: pass --inputs, or define _turbo_bench() in both
  modules`.
- Each side gets its own deep copy of the arguments, so a function that writes
  into a buffer is compared by that buffer. A mutated argument is labelled with
  its parameter name, read from the function's code object rather than from
  `inspect.signature`: on Python 3.14 annotations are evaluated lazily, and
  `signature()` would re-raise `NameError` on `ptr8` after the stub window
  closed.
- Sequences of ints are compared element-wise, reporting the two sums and the
  count differing with max and mean absolute delta; scalars by equality with
  the delta and percentage. The first yield of `cases()` may be a description
  string, printed first.
- Line format is `  <label>   <baseline> -> <candidate>        <delta summary>`,
  then one sentence: `identical output`, or `fixed point moved the answer. this
  is the number a reviewer wants to see.`
- Under `-v`, one caveat: host ints are unbounded and viper ints wrap at 32
  bits, so a host match is necessary, not sufficient. `bench` on a board is the
  final word.
- Exit 0 whether or not they differ; 2 on harness errors. Differing is
  information, not failure.

`examples/mandelbrot/src/mandel_float.py` and `examples/mandelbrot/cases.py`
ship with the package so the command above runs out of the box.

### 5.6 `turbo watch [SRC=src]`

Polls `src/*.py` mtimes every 0.5 s, no inotify dependency. On change it
rebuilds that module under `build`'s rules, copies it as `build` does, and
prints one line.

```
$ turbo watch
watching src/  ->  /media/sklarm/CIRCUITPY5/lib/turbo/armv6m/
board resumed; autoreload is on
09:05:21  pixels.py changed   rebuilt 2 variants   copied   board reloaded
09:05:29  blend.py changed    FAILED src/blend.py:5   ViperTypeError: can't...   copied as native
```

Timestamp, then `<name> changed` padded to 20, then the parts joined by three
spaces.

**The board must be resumed first.** Entering the REPL, which the probe does,
turns CircuitPython's autoreload off, so a copied file reaches the drive and
the running program never notices. `watch` sends Ctrl-D after the probe and
says so. Measured with a control on an RP2040: a reload is seen while the
program runs, not seen after a probe, and seen again after Ctrl-D.

`board reloaded` is printed only when a port is open and the `soft reboot`
banner arrives within 3 s; otherwise the line ends at `copied`. The port's
input buffer is cleared just before each copy, so only a reload caused by that
write counts. A module whose viper tier failed but whose native tier compiled
says `copied as native`, because it did ship. A drive that refuses a write gets
the section 6 sentence and watching continues. Ctrl-C exits 0. Requires a
mount.

### 5.7 `turbo bench MODULE`

Times every candidate on the board and installs the winner. It drives the raw
REPL helper (7.3), so no CircuitPython checkout is needed; `--port` and
`--mount` autodetect. One candidate at a time is placed under the arch
directory, the VM is soft reset so the import cache cannot serve a stale
module, and `_turbo_bench()` is timed `--trials` times. A compiled tier wins
only if it beats the bytecode median by `--min-gain`, and any variant whose
value differs from the bytecode run is rejected. The module's files on the
board are snapshotted first and put back if the run is interrupted.

### 5.8 `turbo check`, `turbo pack`

`check` compares each manifest entry's `sha256` against its source and reports
fresh or STALE. `pack` stages the project, drops the unpicked candidates and
hands the result to `folder2uf2`, either combined with firmware or as a
self-extracting `code.py`.

## 6. Failure sentences (verbatim)

Print the first line, then the explanation and fix indented three spaces.

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

mpy-cross 10.3.0 reports mpy v6.2, board wants v6.3; refusing to use it
   Different .mpy format. Deleted.
   Pass --mpy-cross PATH with a matching build.

no board found
   No CIRCUITPY drive and no serial port. Plug the board in, or pass
   --mount DIR and --port TTY, or --arch NAME to build without a board.

no serial port found
   Reading version and board from /Volumes/CIRCUITPY/boot_out.txt.
   Arch guessed from board id. Loader presence unknown until a port is found.

port /dev/cu.usbmodem1301   busy, another program has it open
   Close the serial monitor (Mu, Thonny, screen, a browser web workflow
   page) and run turbo doctor again.

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
against the last five entries; print the matching hint or nothing. A wrong hint
is worse than none.

## 7. Board detection and the serial probe

### 7.1 Mount

Search in order: `--mount`; `$CIRCUITPY_MOUNT`; on macOS `/Volumes/CIRCUITPY*`;
on Linux `/media/*/CIRCUITPY*`, `/run/media/*/CIRCUITPY*`, `/mnt/CIRCUITPY*`;
on Windows every drive letter whose volume label is `CIRCUITPY`. **The trailing
wildcard matters:** a second board mounts as `CIRCUITPY 1` on macOS and
`CIRCUITPY1` on Linux, and without it a host with several boards attached sees
exactly one. A path counts only if it holds a readable `boot_out.txt`. With
several, prefer the one whose `Board ID` matches `--board`, else the first, and
say `drive       ...  (9 found, using the first; pass --mount)`.

### 7.2 Port

`serial.tools.list_ports.comports()`, keeping entries with VID `0x239A`
(Adafruit) or a description naming CircuitPython. On macOS prefer
`/dev/cu.usbmodem*` over `/dev/tty.*`. A board with two CDC ports lists the
REPL first and the data port second, so the caller tries them in order. The
`boot_out.txt` UID matches `comports().serial_number` where the port reports
one, which pins the right board when several are plugged in.

A port that exists but will not open is reported as such, not as a missing
port. On macOS the `cu` device becomes busy when its `tty` twin is held, which
is what a browser web workflow page does.

### 7.3 Raw REPL helper (`turbo_repl.py`)

Named `turbo_repl`, not `repl`, because it installs as a top-level module and
`repl` is too generic to claim in site-packages. pyserial only, no CircuitPython
checkout.

1. Open at 115200, timeout 2 s. Send `\r\x03\x03` to interrupt twice.
2. Send `\r\x01` for raw mode. Read until `raw REPL; CTRL-B to exit\r\n>`. The
   `\r` is also the keypress CircuitPython waits for after `code.py` stops.
3. Send code, then `\x04`. Read `OK`, then to `\x04` (stdout), then to `\x04`
   (stderr), then `>`. One read can span two markers, so the leftovers carry
   over to the next call.
4. Send `\r\x02` to leave raw mode.

Two explicit extras, because nothing else resets a board:

- `soft_reset()` sends `\x04` at the raw prompt and waits for the soft reboot
  banner and then the raw banner, mirroring `pyboard.py`. `bench` is its only
  caller; it is what clears the import cache between candidates. Verified on
  hardware: a global set before the reset is gone after it.
- `resume()` leaves raw mode and sends `\r\x04`, so the board runs `code.py`
  again with autoreload on. `watch` is its only caller (5.6).

Probe code, sent as one exec:

```python
import sys; print(sys.implementation._mpy, sys.implementation.version)
```

Parse the first int. `AttributeError` means the firmware has no `_mpy`, which
is reported as arch 0 with the stock sentence. Timeout 3 s; on timeout print the
`no serial port` sentence and continue with mount-only facts. `bench` sends
`_turbo_bench()` the same way with a 600 s timeout.

## 8. Toolchain fetch and cache

- Cache root: `$TURBO_CACHE` else `~/.cache/turbo`. Binary at
  `<root>/mpy-cross/<VER>/<os-arch>/mpy-cross[.exe]`, mode 755.
- Platform key: `macos-arm64`, `linux-amd64`, `linux-aarch64`,
  `linux-raspbian` (Linux + `armv7l`), `windows`. `Darwin` + `x86_64` gets the
  section 6 sentence and exit 1 unless `--mpy-cross`.
- `<VER>` is the board's version, or its base release tag when the board runs a
  build after one (2.1).
- Fetch with `urllib.request`, 30 s timeout, to a `.part` file, then rename, so
  an interrupted fetch never leaves half a binary in the cache. Print the URL
  while fetching. On 404, print the unpublished-version sentence.
- Validate after fetch: run `<bin> --version` and require `CircuitPython <VER>`
  and an `mpy v<version>.<sub>` matching the board's `_mpy` decode. Anything
  that fails is deleted, since it would emit the wrong format.
- `--mpy-cross PATH` is trusted: no fetch, no validation, only the `--version`
  report.
- `--offline` never fetches; a missing binary prints the URL as a hint and
  exits 1.

## 9. Packaging

- `pyproject.toml`: name `adafruit-turbo`, version `0.1.0`, console script
  `turbo = turbo_cli:main`, dependency `pyserial>=3.5`, `requires-python
  >=3.9`.
- Layout: `cli/turbo_cli.py` and `cli/turbo_repl.py` as top-level modules, and
  `cli/turbo_assets/` as a package holding `shim/turbo.py` and
  `examples/mandelbrot/`, declared as package data. `asset()` finds them in a
  checkout or an install. Verified by installing a built wheel into a clean
  virtual environment and running `turbo init --example` from it.
- Install: `pipx install git+ssh://git@github.com/mikeysklar/turbo-cli.git`
  until a release exists.

## 10. Tests and acceptance

Unit, pytest, no hardware. 136 tests, passing on Python 3.12 and 3.14:

- `_mpy` decode: the five values in 2.3 round-trip to (arch name, "6.3"), and
  the CLI's table agrees with the shim's.
- `boot_out.txt` parse: the 2.4 sample, plus boot.py output appended, a missing
  `Board ID`, and an empty file.
- URL builder: the five platform keys produce the exact paths in 2.1; the two
  unpublished hosts return nothing.
- Version handling: `N.N.N-<count>-g<hash>[-dirty]` resolves to its base tag,
  a release resolves to itself, a prerelease to nothing.
- `rewrite()`: both tiers, and `None` when there is no decorator.
- Hint lookup: each error prefix in section 6 maps to its hint, and an unknown
  error maps to nothing.
- Raw REPL framing against a scripted fake port: banner handshake, stdout and
  stderr split, a reply spanning two markers, timeout wording, soft reset,
  resume, and port filtering and ordering.
- Toolchain fetch against a local HTTP server: cache layout, 404, unreachable
  host, and every validation failure deleting the binary.
- `verify`: the mandelbrot pair reports `407790 -> 407644`.
- Copy: what lands on the board, that a second run writes nothing, and that
  `code.py` is never touched.

Integration, on the eight-board farm (see the `hil-farm` skill). All five ran
green on 2026-09-08 from an empty toolchain cache with no `--mpy-cross`
anywhere; every board was backed up first and restored and md5-verified after.

1. **Stock firmware.** A Metro RP2040 flashed to stock 10.3.0 reports
   `_mpy 0x0306`; `doctor`, `build` and `watch` print the arch-0 sentence and
   exit 1, `init` prints it and lays the project out. The sentence's claim
   holds: `arch=None path=/src checksum=407644` in 8330 ms.
2. **Arch on every board.** RP2040 and M0 `armv6m`; M4, nRF52840, RP2350 and
   STM32F405 `armv7emsp`; ESP32-S2 and S3 `xtensawin`. All ready, exit 0.
3. **Byte-identical builds.** `cmp` against a direct `mpy-cross -march=<arch>`
   run of the rewritten source passes for all eight arches, at the sizes in 4.2.
4. **Provisioning and running.** `turbo init && turbo build` alone, then
   `code.py` copied, prints `checksum=407644` from `/lib/turbo/<arch>` on all
   eight boards, at the times in `docs/shim-test.md`: RP2040 422, M4 463, M0
   1177, nRF52840 843, ESP32-S2 225, RP2350 281, ESP32-S3 186, STM32F405
   437 ms.
5. **Watch.** Two saves, two rebuilds, two reloads seen, and Ctrl-C exits 0.
   Test Ctrl-C through a pty and reset SIGINT to `SIG_DFL` in the child:
   `nohup` and a job backgrounded by a non-interactive shell both leave SIGINT
   ignored, and the child inherits that, which looks exactly like the program
   ignoring Ctrl-C.

## 11. Out of scope

- A flat `lib/<mod>.mpy` layout without the shim.
- Per-line blockers in `analyze`.
- Splitting a function out of `code.py`; that is the web page's job.
- circup integration, bundle publishing, a `boot_out.txt` arch line, all
  upstream.
- Any web or wasm work; see `turbo-web.md`.

## 12. Files to read first

- `cli/turbo_cli.py` and `cli/turbo_repl.py`
- `cli/turbo_assets/shim/turbo.py`
- `cli/turbo_assets/examples/mandelbrot/`
- In `mikeysklar/turbo`: `docs/analyze.md`, `docs/shim-test.md`,
  `docs/turbo-on-the-farm.md`, `tools/farm/deploy-test.sh`
- `/Volumes/clear/Downloads/CLAUDE.md`, the working rules

## 13. Where the draft was wrong

The 2026-09-07 draft was written before any of this ran. Nine things in it did
not survive contact with hardware. They are recorded here rather than left as
scattered amendments, because each one is a rule someone could reasonably
reinvent.

| What the draft said | What is true |
|---|---|
| Fetch the board's exact version, never substitute | Every turbo firmware is a build after a release tag, so that made the fetch unreachable on turbo's own boards. Fall back to the base tag, announced, with the abi still checked (2.1) |
| `build` copies to `lib/turbo/<arch>/` | That leaves a board that cannot import anything, because the shim is what puts the arch directory on `sys.path`. It copies the shim and the source fallback too (5.3) |
| Pass the temp source path to `mpy-cross` | It embeds the path, so builds were not reproducible and board tracebacks named a temp directory. Run from inside the temp dir with a bare basename (4.2) |
| `verify` reports `581 -> 576` | Never run. The real numbers are `407790 -> 407644` with 335 of 19,200 pixels differing (5.5) |
| `watch` copies and the board autoreloads | The probe leaves the board in the REPL, where autoreload is off. `watch` must resume it with Ctrl-D first (5.6) |
| No arch means no board | A stock board is found, mounted and answering; it just has no loader. `build`, `watch` and `init` print doctor's diagnosis instead of sending the user to check a cable (5.3, 5.2) |
| Search `/media/*/CIRCUITPY` | Numbered drives are invisible without a trailing wildcard; a farm host with nine boards saw one (7.1) |
| A missing port is a missing port | A port that exists but will not open is the common case, and has a different fix (7.2) |
| Nothing about read-only drives | A CIRCUITPY drive can go read-only under the tool. A successful compile then ended in a traceback (5.3) |

Two harness traps cost real time and are worth stating outright. SIGINT is
inherited as ignored from `nohup`, so a Ctrl-C test must use a pty and reset the
disposition in the child. And on macOS, holding a `/dev/cu.*` device exclusively
does not make it busy; holding its `/dev/tty.*` twin does.
