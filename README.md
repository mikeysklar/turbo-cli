# turbo CLI

Host-side tool for turbo. It compiles `@turbo`-decorated CircuitPython modules
to native `.mpy` with the official Adafruit `mpy-cross`, installs them under
`lib/turbo/<arch>/`, puts the shim and the source fallback on the board, and
benches the candidates.

Private during development. Design and implementation spec: `SPEC.md`.

```sh
pipx install git+ssh://git@github.com/mikeysklar/turbo-cli.git
turbo doctor          # board, arch, toolchain, and whether a build could run
turbo init --example  # lib/turbo.py, src/, lib/turbo/<arch>/, the mandelbrot demo
turbo build           # compile, install, copy to the board
turbo watch           # rebuild and copy on every save
```

`turbo doctor` tells you which of the two things below you are missing: the
`mpy-cross` binary, which it fetches for you, or firmware with the native
loader, which it cannot.

## Firmware the board needs

Stock CircuitPython reports `_mpy 0x0306`, arch 0, and will not load a compiled
module. Your code still runs from `/src` as bytecode, just slowly: on a Metro
RP2040 the mandelbrot example takes 8330 ms from source against 422 ms
compiled.

The board needs the **native loader**, not the on-board emitter. The emitter
compiles Python to machine code on the board and costs 20 to 50 KB; turbo never
uses it, because `mpy-cross` on your laptop does that job. The loader is 2 to
3 KB and relocates and calls machine code that is already in the `.mpy`.

```make
CIRCUITPY_LOAD_NATIVE ?= 0                          # py/circuitpy_mpconfig.mk:318
CFLAGS += -DMICROPY_LOAD_NATIVE=$(CIRCUITPY_LOAD_NATIVE)
```

Set `CIRCUITPY_LOAD_NATIVE=1`. Do not use `CIRCUITPY_ENABLE_MPY_NATIVE`, which
is the old flag and builds the whole emitter in.

### Branches

All on the fork `git@github.com:mikeysklar/circuitpython.git`. The first four
are one stack, each branch containing the ones above it, so the tip builds
everything in the table except the M0 and the RA8D1.

| Branch | Head | What it adds | Boards it turns on |
|---|---|---|---|
| `backport-load-native` | `d99feb6aa9` | the five `py/` commits: detect the target arch from compiler defines, decouple loading from the emitters' presence, flush caches when loading, disable native loading in `mpy-cross` | none by itself |
| `load-native-plumbing` | `bcb8e71199` | the `CIRCUITPY_LOAD_NATIVE` flag and the board switches | Metro M4 AirLift, Metro RP2040, Metro RP2350, Feather nRF52840, Feather STM32F405 |
| `ptr-callable-thumb-only` | `6c2d2ac418` | stops setting the Thumb interworking bit on non-ARM pointers | required by every non-ARM port |
| `esp-load-native` | `53be73f801` | commit machine code into executable RAM, memory protection off, the windowed-Xtensa prelude split | adds Metro ESP32-S2, Metro ESP32-S3, ESP32-C5 DevKitC |
| `thumb-armv7m-default` | `3cdb20693f` | one commit: configure the thumb2 and float emitter features automatically | independent of the stack |
| `loader-only-native` | `a80fa21afb` | the original branch, five commits off the `10.3.0` tag | adds Metro M0 Express; the SAMD21 only fits with `safemode.py` dropped, which is why it is not in the upstream stack |
| `esp32-native` | `8c69e71fa9` | the original ESP branch, eleven commits off `10.3.0` | Metro ESP32-S2 and S3 |
| `ra8d1-turbo` | `3896c21387` | one commit, Zephyr | EK-RA8D1, Cortex-M85 |

What the farm boards actually run, for reference when a result has to be
reproduced:

| Board | Firmware | From |
|---|---|---|
| Metro RP2040 | `10.3.0-42-g3cdb20693f` | `thumb-armv7m-default` |
| Metro M4, Feather nRF52840, Metro RP2350, Feather STM32F405 | `10.3.0-48-g799278aeb8` | `load-native-plumbing`, a commit since rebased away |
| Metro ESP32-S2, Metro ESP32-S3 | `10.3.0-53-ge3141eabbd` | `esp-load-native` |
| Metro M0 Express | `10.3.0-1-gaf32cbcb36-dirty` | `loader-only-native` |

A rebuild gives a different `git describe` string than the ones above, since
the branches move. That is fine and is why `turbo` keys on the base release tag
and the `.mpy` abi rather than the exact version.

### Build

Toolchain and submodules are the standard CircuitPython ones; see the Adafruit
build guide. Two things bite: `arm-none-eabi-gcc` 13.2.1 fails with a pragma
error rather than a version message, so use 14.x, and the ESP32 build needs the
IDF exported into the shell first.

```sh
git clone git@github.com:mikeysklar/circuitpython.git && cd circuitpython
git checkout esp-load-native          # tip of the stack, ARM and ESP32
make fetch-all-submodules
make -C mpy-cross -j"$(nproc)"
```

ARM. The five boards in the table carry the flag in their own
`mpconfigboard.mk`, so nothing extra is needed:

```sh
make -C ports/raspberrypi BOARD=adafruit_metro_rp2040 -j"$(nproc)"
make -C ports/nordic      BOARD=feather_nrf52840_express -j"$(nproc)"
make -C ports/stm         BOARD=feather_stm32f405_express -j"$(nproc)"
make -C ports/atmel-samd  BOARD=metro_m4_airlift_lite -j"$(nproc)"
```

Any other ARM board takes the flag on the make line:

```sh
make -C ports/raspberrypi BOARD=adafruit_feather_rp2040 CIRCUITPY_LOAD_NATIVE=1 -j"$(nproc)"
```

ESP32, from the same checkout. The S2, S3 and C5 carry the flag in their board
files as well, so again nothing extra on the make line:

```sh
source ports/espressif/esp-idf/export.sh
make -C ports/espressif BOARD=adafruit_metro_esp32s3 -j"$(nproc)"
```

Metro M0 Express, which needs the other branch:

```sh
git checkout loader-only-native
make -C ports/atmel-samd BOARD=metro_m0_express -j"$(nproc)"
```

### Check the image before flashing

A loader-only build has the relocator and not the emitter. Both numbers matter:
one symbol present, the other absent.

```sh
d=ports/raspberrypi/build-adafruit_metro_rp2040
arm-none-eabi-nm $d/firmware.elf | grep -c ' mp_native_relocate$'   # 1
arm-none-eabi-nm $d/firmware.elf | grep -c emit_native_thumb        # 0
```

On ESP32, also confirm memory protection is off, or the first call into
compiled code faults:

```sh
grep -c '^CONFIG_ESP_SYSTEM_MEMPROT=y' ports/espressif/build-adafruit_metro_esp32s3/esp-idf/sdkconfig  # 0
```

### Flash

RP2040 and RP2350: 1200-baud touch on the board's serial port drops it into the
UF2 bootloader, no button needed, then copy `firmware.uf2` onto the
`RPI-RP2` or `RP2350` volume.

```sh
python3 -c "import serial,time; s=serial.Serial('/dev/ttyACM0',1200); time.sleep(0.2); s.close()"
cp ports/raspberrypi/build-adafruit_metro_rp2040/firmware.uf2 /media/$USER/RPI-RP2/
```

SAMD and nRF52840 boards take the same UF2 route through their own bootloader
volume. The Feather STM32F405 has no UF2 bootloader; flash it over SWD, or
through the ST ROM DFU at `0x1FFF0000`. ESP32 boards take `firmware.uf2` on the
TinyUF2 volume, or the combined `.bin` at `0x0` with `esptool`; run one esptool
command per entry into download mode, because the chip returns to CircuitPython
within seconds of esptool exiting.

### Confirm it took

```sh
turbo doctor
```

```
_mpy        0x1306   arch armv6m · mpy 6.3 · native loader present
ready       turbo build compiles -march=armv6m
```

`0x0306` there means the flag did not make it into the image. The arch values
are in `SPEC.md` section 2.2, and the decode is in 2.3.
