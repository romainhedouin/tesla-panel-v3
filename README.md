# tesla-panel-v3

<img width="1920" height="1445" alt="image" src="https://github.com/user-attachments/assets/6d326fa8-c928-4637-9709-755f334a93fb" />

Firmware for the 64x32 RGB LED panel mounted in the car's rear window. An
ESP32 on a HUB75 adapter board drives the panel and takes commands over
Bluetooth from the [TeslaLED](https://github.com/romainhedouin/TeslaLED)
Android app.

This is v3, ESP32 only. The Raspberry Pi version lives in
[romainhedouin/tesla-panel](https://github.com/romainhedouin/tesla-panel).

## Quick start

Plug the ESP32 into the computer over USB, then from the repo root:

```
./install.sh                                          # macOS / Linux
powershell -ExecutionPolicy Bypass -File install.ps1  # Windows
```

The script installs Python if needed (asking first), then walks through
seven steps with time estimates: check the computer, set up PlatformIO in
`.venv/`, find the board, build, optional wiring check, flash, and read the
board's boot messages to show its Bluetooth address. On an ESP32 it asks
which connection you want: Bluetooth Classic (`esp32-classic`, the default)
or BLE (`esp32-ble`).

**The first run takes 10-15 min** (about 1GB of compiler tools downloaded
into `~/.platformio`); later runs take about a minute. Ctrl-C and re-running
are safe. The full output goes to `install.log`.

| Option | Effect |
|---|---|
| `--env {esp32-classic,esp32-ble}` | flash this firmware instead of asking |
| `--port PORT` | serial port of the board instead of detecting it |
| `--test-pattern` | first flash the test pattern to check the wiring |
| `-y`, `--yes` | never ask |
| `--verbose` | also show the full PlatformIO output |
| `--build-only` | build without flashing |
| `--skip-verify` | don't read the boot messages after flashing |
| `--dry-run` | show the plan and the boards found, change nothing on the board |

## Bill of materials

- **ESP32-DevKitC V4** ([I bought this set of 2](https://www.amazon.fr/dp/B0F65G3V81)).
  Other ESP32 variants (S2, S3, C3...) aren't supported.
- **HUB75 adapter**: seengreat
  ["RGB Matrix Adapter Board (E)"](https://www.amazon.fr/dp/B0FVGCF1RW),
  **rev 2.x** (printed on the board - rev 1.x uses different pins).
- **P4 indoor RGB LED matrix, 64x32**:
  [reference photo](https://ae-pic-a1.aliexpress-media.com/kf/HTB1wbN2NVXXXXcDXVXXq6xXFXXX2.jpg)
  of the one used (no longer listed);
  [this listing](https://fr.aliexpress.com/i/32250605175.html?gatewayAdapt=glo2fra)
  looks similar but is unverified.
- **5V 4A+ power supply** into the adapter's DC jack, with a
  [1m 5.5x2.1mm cable](https://www.amazon.fr/dp/B0FPKWNFLN).
- **A USB cable that carries data** (not charge-only) for flashing.
- Mounting (the Model 3's rear window is laterally sloped): M3 screws +
  washers, a 2-hole bracket (2.5cm between the outer holes), a 4-hole
  bracket (4.5cm), 2 small carabiners and 2 small suction cups with a loop.

## Setup

1. With everything unpowered, seat the ESP32 in the adapter, plug the
   ribbon cable into the adapter's HUB75 port and the panel's **input**
   connector (the arrows on the panel point away from it), and connect the
   panel's power lead (red = +5V, black = GND) to the adapter's 5V output.
2. Power the adapter (5V, 4A+). The ESP32's USB only powers the ESP32.
3. Run the installer (add `--test-pattern` the first time).
4. **esp32-classic**: pair with **`teslapi-esp32`** in Android's Bluetooth
   settings (Android then says "Can't connect" - that's normal).
   **esp32-ble**: don't pair; the app connects directly.
5. In TeslaLED, tap the transport button in the top bar -> **ESP32
   Standard** or **ESP32 BLE**, and enter the Bluetooth address the
   installer printed.

## Environments

| Environment | Transport | Device name | Status |
|---|---|---|---|
| `esp32-classic` | Bluetooth Classic (SPP) | `teslapi-esp32` | Works end to end |
| `esp32-ble` | BLE GATT | `teslapi-esp32-ble` | Works end to end |
| `esp32-test-pattern` | none | - | Wiring/timing check |
| `native` | none | - | Protocol unit tests on the host |

Boot messages (115200 baud) end with `[+] Bluetooth address: AA:BB:...`
and `[+] Ready`. The installer parses those two lines, so keep their format.

## Wire protocol

Defined in `src/protocol.h`, matched byte-for-byte by the app:

```
[1 byte command type][4 bytes big-endian payload length][payload]
```

answered by `[1 byte status][4 bytes big-endian length][UTF-8 message]`:
`0x00` is OK, anything else is an error whose message the app shows as is.

| Command | Value | Payload |
|---|---|---|
| `COMMAND_IMAGE` | 0 | a 64x32 P6 PPM frame (~6.2KB) |
| `COMMAND_KILL` | 2 | none - clears the panel |
| `COMMAND_SET_BRIGHTNESS` | 3 | 1 byte, 1-100 |

Payloads are capped at 8KB; a larger length means a desynced stream and
the firmware drops the connection.

- **Classic**: RFCOMM on the standard SPP UUID
  (`00001101-0000-1000-8000-00805F9B34FB`), a reliable ordered stream.
- **BLE**: service `c9af0000-1a1a-4e7e-9a1e-4b1e2f6a9c00`, command
  characteristic `...9c01` (write, phone -> board), response
  characteristic `...9c02` (notify). The phone splits commands across as
  many writes as the MTU needs; the firmware reassembles them.

## Things to know

- **Pin mapping** (`src/adapter_pins.h`) matches none of the HUB75 library
  defaults and differs between adapter revisions
  ([seengreat wiki](https://seengreat.com/wiki/186/rgb-matrix-adapter-board-e)).
- **Clock phase is flipped** (`clkphase = false` in `src/panel.h`):
  with the default, white pixels fringe into neighbouring columns. The test
  pattern's thin white lines show it (`-DTEST_CLKPHASE=true` to compare).
- **The Arduino core is pinned to 3.3.12** (pioarduino): the stock 2.0.17
  can't complete Bluetooth discovery with Android 17, and 4.0 drops
  `BluetoothSerial` by default. The `BluetoothSerial` deprecation warning
  is expected.
- **RAM is tight** - Bluetooth crashes on connect if it runs short. So
  Bluetooth starts before the panel, the panel is single-buffered, and
  payloads are capped at 8KB. Free heap is printed at boot.
- **Incoming SPP data bypasses `BluetoothSerial`'s 512-byte queue**, which
  silently drops overflow, into an 8KB buffer.

## Troubleshooting

- **Static/noise instead of the image**: wiring or timing, not software.
  Run `./install.sh --test-pattern`: you should see a white border, white
  vertical lines on the left half and red/green/blue dots on the right.
  A blank panel usually means no panel power.
- **The app can't connect**: check the transport (ESP32 Standard vs ESP32
  BLE) and the address. For BLE, don't pair in the phone's settings.
- **The installer can't find the board**: try a cable that carries data,
  another port, no hub. The installer says if a driver (Windows) or the
  `dialout` group (Linux) is missing.
- **Flashing fails with "Failed to connect"**: hold the BOOT button while
  it retries, and close anything else using the port.
- **Anything else**: see `install.log`.

## Development

```
src/main_classic.cpp       esp32-classic entry point (SPP)
src/main_ble.cpp           esp32-ble entry point (BLE GATT)
src/main_test_pattern.cpp  esp32-test-pattern
src/protocol.h             wire protocol
src/panel.h                HUB75 wrapper
src/adapter_pins.h         adapter rev 2.x pin mapping
test/                      Unity tests for protocol.h (native env)
tools/install.py           the installer; install.sh/.ps1 bootstrap Python
```

```
pio test -e native                   # unit tests, no hardware
pio run -e esp32-classic -t upload   # build and flash
pio device monitor -b 115200         # boot/connect/error messages
```

CI runs the unit tests, builds every ESP32 environment and smoke-checks the
installer. The panel is driven by
[ESP32-HUB75-MatrixPanel-DMA](https://github.com/mrcodetastic/ESP32-HUB75-MatrixPanel-DMA),
built with `NO_GFX`.
