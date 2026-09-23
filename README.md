# tesla-panel-v3

Firmware for the 64x32 RGB LED panel mounted in the car's rear window. An
ESP32 on a HUB75 adapter board drives the panel and takes commands over
Bluetooth from the [TeslaLED](https://github.com/romainhedouin/TeslaLED)
Android app (images, clear, brightness).

This is v3, ESP32 only. The Raspberry Pi version lives in
[romainhedouin/tesla-panel](https://github.com/romainhedouin/tesla-panel).

## Quick start

Plug the ESP32 into the computer over USB, then from the repo root:

```
./install.sh                                          # macOS / Linux
powershell -ExecutionPolicy Bypass -File install.ps1  # Windows
```

(`.\install.ps1` also works on Windows if script execution is already
allowed. The `-ExecutionPolicy Bypass` form only affects that one run.)

The shell scripts only make sure Python 3.9+ is available (offering to
install it, and asking before anything needs `sudo`/`winget`), then hand
over to `tools/install.py`, which does the same thing on every OS:

1. **Check your computer** - Python, disk space, network.
2. **Set up PlatformIO** - into a local `.venv/` (PlatformIO 6.x and
   esptool 4.x), rebuilt automatically if a previous run left it
   half-installed.
3. **Find the board** - lists USB serial ports only; asks if there are
   several.
   Then checks with esptool that it really is an ESP32, and asks which
   connection you want: Bluetooth Classic (`esp32-classic`, the default
   and the one used with `--yes`) or BLE (`esp32-ble`).
4. **Build the firmware** for that environment.
5. **Wiring check** (only with `--test-pattern`) -
   flashes the test pattern and asks whether you see it.
6. **Flash the board.**
7. **Check that it started** - resets the board, reads its boot messages
   for up to 15s and shows the Bluetooth address to enter in the app.

It prints the plan with time estimates first, then `[k/7]` and an overall
percentage per step. **The first run takes 10-15 min**: PlatformIO
downloads about 1GB of compiler tools into `~/.platformio` (needs ~1.5GB
free). Later runs take about a minute. Ctrl-C is safe at any point, and
so is running it again.

Everything, including the full PlatformIO/esptool output, goes to
`install.log` at the repo root (overwritten each run).

| Option | Effect |
|---|---|
| `--env {esp32-classic,esp32-ble}` | flash this firmware instead of asking |
| `--port PORT` | serial port of the board (e.g. `/dev/ttyUSB0`, `COM3`) instead of detecting it |
| `--test-pattern` | first flash the test pattern to check the panel wiring, then the real firmware |
| `-y`, `--yes` | never ask: pick the only board found, fail if a choice is ambiguous |
| `--verbose` | also show the full PlatformIO output |
| `--build-only` | build without flashing (with `--env`, no board needed) |
| `--skip-verify` | don't read the board's boot messages after flashing |
| `--dry-run` | show the plan and the boards found without building, flashing or opening the port (still installs PlatformIO into `.venv`) |

## Bill of materials

- **ESP32 board**: [ESP32-DevKitC V4](https://www.espressif.com/en/products/devkits/esp32-devkitc)
  (original ESP32 - its radio does both Bluetooth Classic and BLE). Other
  ESP32 variants (S2, S3, C3...) aren't supported: the pin mapping and
  Bluetooth setup are specific to this chip.
- **HUB75 adapter**: seengreat
  ["RGB Matrix Adapter Board (E)"](https://www.amazon.fr/dp/B0FVGCF1RW),
  **rev 2.x** (printed on the board). Rev 1.x uses different pins.
- **P4 indoor full-colour RGB LED matrix, 64x32**
  - [Reference photo](https://ae-pic-a1.aliexpress-media.com/kf/HTB1wbN2NVXXXXcDXVXXq6xXFXXX2.jpg)
    of the one used - no longer listed on AliExpress
  - [This listing](https://fr.aliexpress.com/i/32250605175.html?gatewayAdapt=glo2fra)
    looks similar but is unverified
- **5V power supply, 4A or more**, into the adapter's USB-C or DC jack. The
  ESP32's own USB only powers the ESP32, not the panel.
  - Thinner/longer cables drop more voltage under load, risking
    under-voltage and visible brightness variation. Prefer short, thick
    cables - the Pi build used a 2m 22 AWG
    [5.5x2.1mm cable](https://www.amazon.fr/dp/B07KFTPF4C) and would have
    been better off with [a 1m one](https://www.amazon.fr/dp/B0FPKWNFLN).
    Check the adapter's DC jack size before buying.
- **A USB cable that carries data** (not charge-only) for flashing.
- Mounting - the Tesla Model 3's rear window is laterally sloped:
  - M3 screws + washers
  - 2-hole bracket, 2.5cm between the external holes
  - 4-hole bracket, 4.5cm between the external holes
  - 2 small carabiners, attached to the brackets
  - 2 small suction cups with a loop, attached to the carabiners

## Setup

1. With everything unpowered, seat the ESP32 in the adapter (the right way
   round) and plug the panel's ribbon cable into the adapter's HUB75 port
   and the panel's **input** connector (the arrows on the back of the panel
   point away from it). Then connect the panel's separate power lead
   (red = +5V, black = GND) to the adapter's 5V output terminal - check
   the markings on your adapter for polarity.
2. Power the panel through the adapter's USB-C or DC jack (5V, 4A+).
3. Plug the ESP32 into the computer and run the installer (see
   [Quick start](#quick-start)). Add `--test-pattern` the first time to
   check the wiring before the real firmware goes on.
4. Pair the phone:
   - **esp32-classic**: pair with **`teslapi-esp32`** in Android's
     Bluetooth settings. Android then says "Can't connect" - that's
     normal, it only means the board has no audio profile.
   - **esp32-ble**: don't pair it in the settings; the app connects
     directly.
5. In TeslaLED, tap the transport button in the top bar (it shows PI,
   ESP32 or BLE) -> **ESP32 Standard** (classic) or **ESP32 BLE**, and
   enter the board's Bluetooth address. The installer prints it at the
   end; it is also in the boot log, and for classic in the phone's
   Bluetooth device details.

## Environments

| Environment | Board | Transport | Device name | Status |
|---|---|---|---|---|
| `esp32-classic` | ESP32-DevKitC V4 | Bluetooth Classic (SPP) | `teslapi-esp32` | **Works end to end** (adapter rev 2.2, 64x32 panel, Pixel 9) |
| `esp32-ble` | ESP32-DevKitC V4 | BLE GATT | `teslapi-esp32-ble` | **Works end to end** (adapter rev 2.2, Pixel 9) |
| `esp32-test-pattern` | ESP32-DevKitC V4 | none | - | Static diagnostic pattern, quickest wiring/timing check |
| `native` | host | none | - | Protocol unit tests |

Boot messages, over the USB serial port at 115200 baud:

```
[+] Free heap: N bytes, largest block: M
[+] Bluetooth SPP server started as "teslapi-esp32"   (BLE: [+] BLE GATT server started as "teslapi-esp32-ble")
[+] Bluetooth address: AA:BB:CC:DD:EE:FF
[+] Ready
```

The test pattern prints `[+] Diagnostic pattern, clkphase=0` then
`[+] Ready`. The installer parses the `Bluetooth address` and `Ready`
lines, so keep their format (uppercase hex, colon-separated; `Ready` last
in `setup()`) if you touch them.

## Wire protocol

Defined in `src/protocol.h`, matched byte-for-byte by the Android app:

```
[1 byte command type][4 bytes big-endian payload length][payload]
```

followed by a response in the same shape - `[1 byte status][4 bytes
big-endian message length][message, UTF-8]`. `0x00` is OK (message
normally empty); anything else is an error, with the message carrying the
firmware's own explanation - the app shows that text directly (e.g. as an
"ERROR: ..." toast) instead of guessing at a reason.

| Command | Value | Payload |
|---|---|---|
| `COMMAND_IMAGE` | 0 | a 64x32 P6 PPM frame (~6.2KB) |
| `COMMAND_KILL` | 2 | none - clears the panel |
| `COMMAND_SET_BRIGHTNESS` | 3 | 1 byte, 1-100 |

Payloads are capped at **8KB** (`MAX_PAYLOAD_SIZE`, the Pi allowed 64KB). A
larger announced length means a desynced stream: the firmware drops the
connection and the app reconnects from a clean state.

**Classic (SPP)**: RFCOMM on the standard SPP UUID
(`00001101-0000-1000-8000-00805F9B34FB`). It is a reliable ordered stream,
so the length prefix is all that's needed to find message boundaries - no
sentinel, no per-chunk acks.

**BLE (GATT)**: a custom service, since BLE has no generic serial profile:

| | UUID | Properties |
|---|---|---|
| Service | `c9af0000-1a1a-4e7e-9a1e-4b1e2f6a9c00` | |
| Command (phone -> board) | `c9af0000-1a1a-4e7e-9a1e-4b1e2f6a9c01` | write, write without response |
| Response (board -> phone) | `c9af0000-1a1a-4e7e-9a1e-4b1e2f6a9c02` | notify |

Same framing, but a write is capped by the negotiated MTU (the firmware
requests 517), far below an image frame, so the phone splits the byte
stream across as many command writes as it needs. Chunks don't have to line
up with command boundaries: the firmware reassembles into a fixed
header+8KB buffer, dispatches each command as soon as it is complete and
carries any extra bytes into the next one. Each response is small enough
for a single notification.

## Things to know

- **Pin mapping** (`src/adapter_pins.h`) matches none of the HUB75 library
  defaults, and differs between adapter revisions V1.x and V2.x - check
  the revision printed on the board
  ([seengreat wiki](https://seengreat.com/wiki/186/rgb-matrix-adapter-board-e)).
- **Clock phase is flipped** (`clkphase = false` in `src/panel.h`). With
  the library default, white pixels fringed into neighbouring columns
  (colour channels a pixel apart), worst on the bottom half. Solid colours
  hide this; the test pattern's thin white lines show it. Build the test
  pattern with `-DTEST_CLKPHASE=true` to compare.
- **The Arduino core is pinned to 3.3.12** in `platformio.ini` (pioarduino
  platform `55.03.312`). PlatformIO's stock platform ships core 2.0.17,
  which Android 17 (Pixel 9) can't complete Bluetooth service discovery
  against. Core 4.0 stops enabling `BluetoothSerial` by default, so don't
  upgrade blindly. The `BluetoothSerial` deprecation warning at build time
  comes with this core and is expected.
- **RAM is tight.** Bluetooth crashes on connect if it runs short, so:
  Bluetooth initialises before the panel, each build releases the controller
  memory of the radio mode it doesn't use (BLE for classic, Classic for
  BLE), the panel is single-buffered, and payloads are
  capped at 8KB. Free heap and largest block are printed at boot.
- **Incoming SPP data bypasses `BluetoothSerial`'s 512-byte queue**, which
  silently drops overflow, and goes into an 8KB stream buffer instead.
- **BLE advertising starts only once the panel exists**, so an early
  client can't send a command before there is anything to dispatch it to.
- **Serial monitor at 115200** shows boot, connect and error messages.

## Troubleshooting

- **Coloured static/noise instead of the image** is a wiring or signal
  timing issue, not a protocol bug. Run `./install.sh --test-pattern` (or
  flash `esp32-test-pattern`) - no Bluetooth involved. You should see a
  thin white border around the whole panel, thin white vertical lines on
  the left half, and rows of red, green and blue dots on the right half.
  A blank panel with the ESP32's LED on usually means no panel power;
  garbled colours usually mean a loose or reversed ribbon.
- **Android says "Can't connect" after pairing** (classic): normal, see
  [Setup](#setup). The app connects on its own.
- **The app can't connect**: check the transport (ESP32 Standard vs ESP32
  BLE) and the address. For BLE, don't pair the board in the phone's
  settings.
- **The installer can't find the board**: many USB cables are charge-only
  - try one you know carries data, another port, no hub. Windows may need
  the CP210x or CH340 driver; on Linux you need to be in the port's group
  (usually `dialout`), and `brltty` can grab CH340 boards. The installer
  says which applies.
- **Flashing fails with "Failed to connect"**: hold the BOOT (IO0) button
  while it retries, and close anything else using the port (serial
  monitor, Arduino IDE).
- **No `[+] Ready` after flashing**: only a warning - the installer shows
  what the board printed.
- **Anything else**: `install.log` has the full output of the last run.

## Development

```
src/
  main_classic.cpp        esp32-classic entry point (SPP)
  main_ble.cpp            esp32-ble entry point (BLE GATT)
  main_test_pattern.cpp   esp32-test-pattern, no Bluetooth
  protocol.h              wire protocol, transport-agnostic
  panel.h                 HUB75 wrapper, one handler per command
  adapter_pins.h          adapter rev 2.x pin mapping
test/
  test_protocol/          Unity tests for protocol.h (native env)
  shim/Arduino.h          minimal String stand-in for host builds
tools/install.py          the installer (standard library only)
install.sh, install.ps1   Python bootstrap for macOS/Linux and Windows
platformio.ini            environments; build_src_filter picks the entry point
```

The panel side is built on
[ESP32-HUB75-MatrixPanel-DMA](https://github.com/mrcodetastic/ESP32-HUB75-MatrixPanel-DMA),
built with `NO_GFX` (no Adafruit GFX dependency). With PlatformIO installed (`pip install platformio`, or
`.venv/bin/pio` after one installer run):

```
pio test -e native                                     # protocol unit tests, no hardware
pio run -e esp32-classic                               # build one environment
pio run -e esp32-classic -t upload                     # build and flash
pio device monitor -b 115200                           # boot/connect/error messages
```

A bare `pio run` builds `esp32-classic` (`default_envs`).

CI (`.github/workflows/ci.yml`, on push and PR to `master`) runs the native
tests, builds all three ESP32 environments, and smoke-checks the installer
(`py_compile`, `bash -n install.sh`, `install.py --help`). Nothing in CI
touches hardware, and `install.ps1` isn't checked there.
