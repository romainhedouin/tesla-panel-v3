#!/usr/bin/env python3
"""Guided installer for the Tesla LED panel firmware.

Builds the firmware with PlatformIO and flashes it to an ESP32 over USB,
explaining each step as it goes. Meant for people who have never touched
embedded tooling: every step says what it does, roughly how long it takes
and what to do when something goes wrong.

Usually started through ./install.sh (macOS/Linux) or .\\install.ps1
(Windows), which only make sure Python 3.9+ exists. Run it directly with
"python3 tools/install.py --help" to see the options.

Standard library only: PlatformIO, esptool and pyserial are installed into
<repo>/.venv, and everything that needs pyserial runs as a helper
subprocess of this same file inside that venv (see serial_helper()).
"""

import argparse
import json
import math
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import traceback

if sys.version_info < (3, 9):
  sys.exit("This installer needs Python 3.9 or newer (found %d.%d)." % sys.version_info[:2])

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_DIR = os.path.join(REPO_ROOT, ".venv")
LOG_PATH = os.path.join(REPO_ROOT, "install.log")
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# Known-good ranges: PlatformIO 7 may change the CLI, esptool 5 renamed
# commands and reworded the output parsed by detect_chip().
PIP_PACKAGES = ["platformio>=6.1,<7", "esptool>=4.7,<5"]


def _pick_core_dir():
  """Where PlatformIO keeps the toolchain, framework and libraries (~1.5GB
  on first run). Returns (path, moved): PlatformIO's compilers break on
  Windows user folders with accents ("C:\\Users\\Jérôme"), so those get the
  drive root instead - which any Windows user may create."""
  if os.environ.get("PLATFORMIO_CORE_DIR"):
    return os.environ["PLATFORMIO_CORE_DIR"], False
  default = os.path.join(os.path.expanduser("~"), ".platformio")
  if IS_WINDOWS and not default.isascii():
    return os.path.splitdrive(default)[0] + "\\.platformio", True
  return default, False


PIO_CORE_DIR, PIO_CORE_DIR_MOVED = _pick_core_dir()
DISK_NEEDED_FIRST_RUN = 1.5e9
DISK_NEEDED_AFTER = 0.3e9

BAUD = 115200
VERIFY_TIMEOUT = 15

# Firmware environments the user can end up with (platformio.ini), and what
# they mean on the phone side. "app_label" is the TeslaLED transport picker
# entry (MainActivity.TRANSPORT_OPTIONS in the app).
ENVS = {
  "esp32-classic": {
    "chip": "ESP32",
    "board": "ESP32-DevKitC V4",
    "radio": "Bluetooth Classic (SPP)",
    "device_name": "teslapi-esp32",
    "app_label": "ESP32 Standard",
    "tested": True,
  },
  "esp32-s3-ble": {
    "chip": "ESP32-S3",
    "board": "ESP32-S3-DevKitC-1",
    "radio": "Bluetooth Low Energy (BLE)",
    "device_name": "teslapi-esp32-ble",
    "app_label": "ESP32 BLE",
    "tested": False,
  },
}
TEST_PATTERN_ENV = "esp32-test-pattern"

# USB vendor IDs of the USB-to-serial chips found on ESP32 dev boards.
KNOWN_USB_VIDS = {
  0x10C4: "Silicon Labs CP210x",
  0x1A86: "WCH CH340/CH9102",
  0x0403: "FTDI",
  0x303A: "Espressif native USB",
}

# Boot-log contract with the firmware (see src/main_*.cpp).
ADDRESS_RE = re.compile(r"\[\+\] Bluetooth address: ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
READY_RE = re.compile(r"\[\+\] Ready\b")
ANSI_RE = re.compile(r"\033\[[0-9;]*m")

DRIVER_LINKS = {
  "CP210x": "https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers",
  "CH340/CH9102 (Windows)": "https://www.wch-ic.com/downloads/CH341SER_EXE.html",
  "CH340/CH9102 (macOS)": "https://www.wch-ic.com/downloads/CH34XSER_MAC_ZIP.html",
}


class Abort(Exception):
  """Stops the installer with a message the user can act on (no traceback)."""

  def __init__(self, message, hint=None):
    super().__init__(message)
    self.hint = hint


def relpath(path):
  """Path relative to the current directory when that's shorter (never
  fails - Windows can't relate paths on different drives)."""
  try:
    rel = os.path.relpath(path)
  except ValueError:
    return path
  return rel if len(rel) < len(path) else path


def wrap(text, width):
  # Never split words or paths (textwrap breaks "tesla-panel" on the hyphen).
  return textwrap.wrap(text, width, break_long_words=False, break_on_hyphens=False)


def fmt_duration(seconds):
  seconds = int(round(seconds))
  if seconds < 60:
    return "%ds" % seconds
  return "%dm%02ds" % (seconds // 60, seconds % 60)


# ---------------------------------------------------------------------------
# Logging and terminal UI
# ---------------------------------------------------------------------------

class Log:
  """install.log: everything, including the full PlatformIO/esptool output."""

  def __init__(self, path):
    self.path = path
    try:
      self._file = open(path, "w", encoding="utf-8")
    except OSError:
      import tempfile
      self.path = os.path.join(tempfile.gettempdir(), "tesla-panel-install.log")
      self._file = open(self.path, "w", encoding="utf-8")

  def write(self, text):
    self._file.write(text if text.endswith("\n") else text + "\n")
    self._file.flush()

  def note(self, text):
    self.write("## [%s] %s" % (time.strftime("%H:%M:%S"), text))

  def tail(self, count):
    try:
      with open(self.path, encoding="utf-8", errors="replace") as f:
        return [line.rstrip("\n") for line in f.readlines()[-count:]]
    except OSError:
      return []


def _enable_windows_vt():
  """Turns on ANSI escape handling in the Windows console (Win10+)."""
  try:
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
      return False
    return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
  except Exception:
    return False


class UI:
  """Everything printed to the user goes through here: colors, symbols,
  prompts and the single live-updating status line."""

  def __init__(self, log, interactive, why_not=""):
    self.log = log
    self.interactive = interactive
    self.why_not = why_not  # shown next to automatic answers, e.g. "--yes"
    self.tty = sys.stdout.isatty()
    ansi = self.tty and (not IS_WINDOWS or _enable_windows_vt())
    # NO_COLOR (no-color.org): any non-empty value turns colors off.
    self.color = ansi and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
    # Carriage-return redraws need a real terminal; "\033[K" additionally
    # needs ANSI, otherwise the line is padded with spaces instead.
    self.live = self.tty
    self.ansi = ansi
    fancy = "▸✓✗⚠…─│┌┐└┘·"
    try:
      fancy.encode(sys.stdout.encoding or "ascii")
      self.sym = dict(arrow="▸", ok="✓", fail="✗", warn="!", dots="…", h="─", v="│",
                      tl="┌", tr="┐", bl="└", br="┘", sep="·", spin="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
      "⠋".encode(sys.stdout.encoding or "ascii")
      # The classic Windows console (conhost + Consolas) has no braille or
      # check marks and shows them as boxes; Windows Terminal is fine.
      if IS_WINDOWS and not os.environ.get("WT_SESSION"):
        self.sym.update(arrow=">", ok="OK", fail="X", spin="|/-\\")
    except (UnicodeEncodeError, LookupError):
      self.sym = dict(arrow=">", ok="OK", fail="X", warn="!", dots="...", h="-", v="|",
                      tl="+", tr="+", bl="+", br="+", sep="-", spin="|/-\\")
    self._status_visible = False
    self._spin = 0
    self._last_plain_status = (None, 0.0)

  # -- styling --
  def _c(self, code, text):
    return "\033[%sm%s\033[0m" % (code, text) if self.color else text

  def bold(self, text): return self._c("1", text)
  def dim(self, text): return self._c("2", text)
  def green(self, text): return self._c("32", text)
  def yellow(self, text): return self._c("33", text)
  def red(self, text): return self._c("31", text)
  def cyan(self, text): return self._c("36", text)

  @property
  def width(self):
    return max(40, min(shutil.get_terminal_size((80, 24)).columns, 100))

  # -- output --
  def print(self, text="", log=True):
    self.clear_status()
    print(text, flush=True)
    if log:
      self.log.write(ANSI_RE.sub("", text))

  def info(self, text, indent=6):
    for line in wrap(text, self.width - indent - 1) or [""]:
      self.print(" " * indent + line)

  def ok(self, text):
    self.print("      %s %s" % (self.green(self.sym["ok"]), text))

  def warn(self, text):
    lines = wrap(text, self.width - 9)
    self.print("      %s %s" % (self.yellow(self.sym["warn"]), self.yellow(lines[0])))
    for line in lines[1:]:
      self.print("        " + self.yellow(line))

  def error(self, text):
    lines = wrap(text, self.width - 9)
    self.print("      %s %s" % (self.red(self.sym["fail"]), self.red(lines[0])))
    for line in lines[1:]:
      self.print("        " + self.red(line))

  def bullets(self, items, indent=8):
    for item in items:
      wrapped = wrap(item, self.width - indent - 3)
      self.print(" " * indent + "- " + wrapped[0])
      for line in wrapped[1:]:
        self.print(" " * (indent + 2) + line)

  def box(self, title, lines):
    s = self.sym
    inner = min(self.width, 80) - 6  # text width between "| " and " |"
    self.print("  " + s["tl"] + s["h"] + " " + self.bold(title) + " " + s["h"] * (inner - len(title) - 1) + s["tr"])
    for line in lines:
      # Leading spaces are kept; "1. ..." items get a hanging indent.
      pad = len(line) - len(line.lstrip(" "))
      numbered = re.match(r"\d+\. ", line.strip())
      hang = pad + (len(numbered.group(0)) if numbered else 0)
      for i, part in enumerate(wrap(line.strip(), inner - hang) or [""]):
        text = " " * (pad if i == 0 else hang) + part
        self.print("  " + s["v"] + " " + text.ljust(inner) + " " + s["v"])
    self.print("  " + s["bl"] + s["h"] * (inner + 2) + s["br"])

  # -- live status line --
  def status(self, text, key=None):
    """Shows `text` as the single live line under the current step, with a
    spinner. Without a terminal, prints a plain line instead, only when
    `key` (what's worth reporting, e.g. the phase) changes or every 15s."""
    if self.live:
      self._spin += 1
      spinner = self.sym["spin"][self._spin % len(self.sym["spin"])]
      line = "      %s %s" % (self.cyan(spinner), text)
      visible = ANSI_RE.sub("", line)
      if len(visible) > self.width - 1:
        # Too wide would wrap and break the in-place redraw: drop the
        # colors and cut it.
        line = visible[: self.width - 2]
      if self.ansi:
        sys.stdout.write("\r\033[K" + line)
      else:
        sys.stdout.write("\r" + line.ljust(self.width - 1))
      sys.stdout.flush()
      self._status_visible = True
    else:
      key = text if key is None else key
      last_key, last_time = self._last_plain_status
      if key != last_key or time.monotonic() - last_time >= 15:
        print("      ... " + text, flush=True)
        self._last_plain_status = (key, time.monotonic())

  def clear_status(self):
    if self._status_visible:
      if self.ansi:
        sys.stdout.write("\r\033[K")
      else:
        sys.stdout.write("\r" + " " * (self.width - 1) + "\r")
      sys.stdout.flush()
      self._status_visible = False

  # -- prompts --
  def _input(self, prompt):
    if not self.interactive:
      raise Abort("The installer needs an answer here, but it can't ask (%s)." % self.why_not,
                  "Run it from a terminal without --yes, or pass the missing choice as an option "
                  "(--port / --env).")
    self.clear_status()
    try:
      return input(prompt)
    except EOFError:
      raise Abort("The installer needs an answer, but there is no keyboard input (stdin is closed).",
                  "Re-run it from a terminal, or use --yes together with --port / --env.")

  def ask_yes_no(self, question, default, noninteractive=None):
    """`noninteractive` is the answer used with --yes (defaults to `default`)."""
    if not self.interactive:
      answer = default if noninteractive is None else noninteractive
      self.print("      %s %s %s" % (self.cyan("?"), question, self.dim("-> %s (%s)" % ("yes" if answer else "no", self.why_not))))
      return answer
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
      reply = self._input("      %s %s %s " % (self.cyan("?"), question, suffix)).strip().lower()
      self.log.write("## answer: %r" % reply)
      if not reply:
        return default
      if reply in ("y", "yes", "o", "oui"):
        return True
      if reply in ("n", "no", "non"):
        return False
      self.print("        Please answer y or n.", log=False)

  def choose(self, question, options):
    """Numbered menu; returns the chosen index."""
    for i, option in enumerate(options, 1):
      self.print("        %s %s" % (self.bold("%d)" % i), option))
    while True:
      reply = self._input("      %s %s [1-%d] " % (self.cyan("?"), question, len(options))).strip()
      self.log.write("## answer: %r" % reply)
      if reply.isdigit() and 1 <= int(reply) <= len(options):
        return int(reply) - 1
      self.print("        Please type a number between 1 and %d." % len(options), log=False)

  def pause(self, text):
    self._input("      %s %s " % (self.cyan(self.sym["arrow"]), text))


# ---------------------------------------------------------------------------
# Running PlatformIO / esptool with a condensed, live progress line
# ---------------------------------------------------------------------------

class OutputParser:
  """Turns a tool's output lines into (phase text, fraction of the step
  done). The fraction only ever moves forward."""

  def __init__(self):
    self.phase = "Starting" + "..."
    self.fraction = 0.0
    self.started = time.monotonic()

  def feed(self, line):
    pass

  def tick(self):
    """Called a few times per second, even without output (time-based creep)."""

  def advance(self, fraction):
    self.fraction = max(self.fraction, min(fraction, 0.99))


class EsptoolParser(OutputParser):
  def __init__(self):
    super().__init__()
    self.phase = "Opening the port"

  def feed(self, line):
    if "Connecting" in line:
      self.phase = "Connecting (resetting the board into download mode)"
      self.advance(0.3)
    elif "Detecting chip type" in line or line.startswith("Chip is"):
      self.phase = "Reading the chip type"
      self.advance(0.7)


class PipParser(OutputParser):
  def __init__(self):
    super().__init__()
    self.phase = "Preparing"
    self.packages = 0

  def feed(self, line):
    if line.startswith("Collecting "):
      self.packages += 1
      self.phase = "Downloading (%d packages)" % self.packages
      self.advance(0.1 + 0.6 * (1 - math.exp(-self.packages / 25)))
    elif line.startswith("Installing collected packages"):
      self.phase = "Installing packages"
      self.advance(0.8)
    elif line.startswith("Successfully installed"):
      self.phase = "Finishing"
      self.advance(0.99)

  def tick(self):
    self.advance(0.7 * (1 - math.exp(-(time.monotonic() - self.started) / 90)))


class BuildParser(OutputParser):
  """Phases of 'pio run'. The first build downloads ~1GB of tools, so the
  percentage there is time-based; compiling counts object files (~100 for
  this project from scratch)."""

  # (rank, fraction of the step reached when the phase starts). A phase only
  # replaces one of lower rank, so interleaved output (a library download
  # between compiles, the bootloader image built early) can't move it back.
  RANKS = {"download": (1, 0.0), "libraries": (2, 0.40), "resolve": (3, 0.42), "compile": (4, 0.45),
           "link": (5, 0.91), "size": (6, 0.95), "image": (7, 0.97)}

  def __init__(self):
    super().__init__()
    self.phase = "Reading the project"
    self.rank = 0
    self.compiled = 0
    self.downloading_since = None
    self.usage = {}

  def enter(self, name, text):
    rank, fraction = self.RANKS[name]
    if rank >= self.rank:
      self.rank = rank
      self.phase = text
      self.advance(fraction)

  def feed(self, line):
    s = line.strip()
    if s.startswith("Library Manager:"):
      self.enter("libraries", "Installing libraries")
    elif re.search(r"(Platform|Tool|Package) Manager: Installing|^Downloading|^Unpacking", s):
      if self.downloading_since is None:
        self.downloading_since = time.monotonic()
      name = re.search(r"Installing ([\w.@/-]+)", s)
      self.enter("download", "Downloading tools" + (" (%s)" % name.group(1).split("@")[0][:40] if name else ""))
    elif s.startswith("LDF:") or s.startswith("Dependency Graph"):
      self.enter("resolve", "Resolving libraries")
    elif s.startswith("Compiling "):
      self.compiled += 1
      self.enter("compile", "Compiling (%d file%s)" % (self.compiled, "" if self.compiled == 1 else "s"))
      self.advance(0.45 + 0.44 * (1 - math.exp(-self.compiled / 60)))
    elif s.startswith("Linking "):
      self.enter("link", "Linking")
    elif s.startswith("Checking size"):
      self.enter("size", "Checking size")
    elif re.match(r"Building \S*firmware\.bin", s):
      self.enter("image", "Creating the firmware image")
    usage = re.match(r"(RAM|Flash):.*?(\d+(?:\.\d+)?)%", s)
    if usage:
      self.usage[usage.group(1)] = usage.group(2)

  def tick(self):
    if self.downloading_since is not None and self.compiled == 0:
      # Downloads take 1-8 minutes depending on the connection: creep
      # towards 38% so the bar keeps moving without promising anything.
      self.advance(0.38 * (1 - math.exp(-(time.monotonic() - self.downloading_since) / 150)))


class UploadParser(OutputParser):
  """Phases of 'pio run -t upload' (esptool 'write_flash' output)."""

  def __init__(self):
    super().__init__()
    self.phase = "Checking the build"
    self.regions_done = 0

  def feed(self, line):
    s = line.strip()
    if s.startswith("Compiling ") or s.startswith("Linking "):
      self.phase = "Rebuilding"
    elif s.startswith("Serial port") or "Connecting" in s:
      self.phase = "Connecting to the board"
      self.advance(0.15)
    elif s.startswith("Chip is") or "Uploading stub" in s or s.startswith("Changing baud"):
      self.phase = "Connected, preparing to write"
      self.advance(0.2)
    elif s.startswith("Erasing") or s.startswith("Flash will be erased"):
      self.phase = "Erasing"
      self.advance(0.22)
    elif s.startswith("Wrote ") or s.startswith("Hash of data verified"):
      if s.startswith("Wrote "):
        self.regions_done += 1
    elif s.startswith("Hard resetting") or s.startswith("Leaving"):
      self.phase = "Restarting the board"
      self.advance(0.98)
    written = re.search(r"Writing at 0x[0-9a-fA-F]+.*?(\d+(?:\.\d+)?) ?%", s)
    if written:
      pct = float(written.group(1))
      # Bootloader, partition table and boot_app0 are tiny; the app is ~95%
      # of the bytes, so the bar mostly follows the last region.
      if self.regions_done < 3:
        self.advance(0.25 + 0.07 * (self.regions_done + pct / 100) / 3)
        self.phase = "Writing (part %d of 4) %d%%" % (self.regions_done + 1, pct)
      else:
        self.advance(0.32 + 0.64 * pct / 100)
        self.phase = "Writing the firmware %d%%" % pct


def tool_env():
  env = dict(os.environ)
  env.update({
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
    "PLATFORMIO_DISABLE_PROGRESSBAR": "true",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
  })
  if PIO_CORE_DIR_MOVED:
    env["PLATFORMIO_CORE_DIR"] = PIO_CORE_DIR
  return env


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class Step:
  """One numbered step of the plan. `weight` is its share of the total work
  (drives the overall percentage); `estimate(ctx)` is the human duration
  shown in the plan; `skip_reason(ctx)` returns a reason to skip or None."""

  def __init__(self, title, weight, estimate, run, skip_reason=None):
    self.title = title
    self.weight = weight
    self.estimate = estimate
    self.run = run
    self.skip_reason = skip_reason or (lambda ctx: None)


class Installer:
  def __init__(self, args):
    self.args = args
    self.log = Log(LOG_PATH)
    # Without a keyboard (piped stdin, CI) behave as --yes instead of
    # failing at the first prompt.
    if args.yes:
      why_not = "--yes"
    elif not sys.stdin.isatty():
      why_not = "no keyboard input"
    else:
      why_not = ""
    self.ui = UI(self.log, interactive=not why_not, why_not=why_not)
    self.started = time.monotonic()
    self.env = args.env
    self.port = args.port
    self.port_info = None       # dict from list_ports, when detected
    self.chip = None            # e.g. "ESP32-D0WD-V3"
    self.chip_mac = None        # base MAC from esptool, fallback for the address
    self.address = None         # Bluetooth address read back from the board
    self.steps = [
      Step("Check your computer", 1, lambda c: "a few seconds", Installer.step_check),
      Step("Set up PlatformIO", 15, Installer.estimate_setup, Installer.step_setup),
      Step("Find the board", 4, lambda c: "a few seconds", Installer.step_find_board, Installer.skip_find_board),
      Step("Build the firmware", 55, Installer.estimate_build, Installer.step_build, Installer.skip_build),
      Step("Wiring check (test pattern)", 20, lambda c: "about 2 min, needs you to look at the panel",
           Installer.step_test_pattern, Installer.skip_test_pattern),
      Step("Flash the board", 15, lambda c: "about 1 min", Installer.step_flash, Installer.skip_flash),
      Step("Check that it started", 10, lambda c: "up to %ds" % VERIFY_TIMEOUT, Installer.step_verify,
           Installer.skip_verify),
    ]
    self.skips = {}
    self.done_weight = 0.0
    self.total_weight = 1.0
    self.current = None

  # -- paths --
  @property
  def venv_python(self):
    if IS_WINDOWS:
      return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")

  def pio(self, *args):
    # "python -m platformio" rather than the pio(.exe) launcher: same on
    # every OS, and never picks up a globally installed PlatformIO.
    return [self.venv_python, "-m", "platformio"] + list(args)

  # -- first-run detection (for honest time estimates) --
  def toolchain_present(self):
    packages = os.path.join(PIO_CORE_DIR, "packages")
    return os.path.isdir(os.path.join(packages, "framework-arduinoespressif32")) and any(
      name.startswith("toolchain-xtensa") for name in (os.listdir(packages) if os.path.isdir(packages) else []))

  def venv_ready_marker(self):
    """Cheap guess (no subprocess) that .venv already has PlatformIO."""
    if not os.path.exists(self.venv_python):
      return False
    if IS_WINDOWS:
      site = [os.path.join(VENV_DIR, "Lib", "site-packages")]
    else:
      lib = os.path.join(VENV_DIR, "lib")
      site = [os.path.join(lib, d, "site-packages") for d in (os.listdir(lib) if os.path.isdir(lib) else [])]
    return any(os.path.isdir(os.path.join(d, "platformio")) for d in site)

  def estimate_setup(self):
    return "a few seconds (already set up)" if self.venv_ready_marker() else "1-3 min, first run only"

  def estimate_build(self):
    if not self.toolchain_present():
      return "3-10 min the first time (downloads ~1GB of tools), seconds afterwards"
    built = self.env and os.path.exists(os.path.join(REPO_ROOT, ".pio", "build", self.env, "firmware.bin"))
    return "a few seconds (already built once)" if built else "1-3 min"

  # -- skip rules --
  def skip_find_board(self):
    if self.args.build_only and self.env:
      return "not needed for --build-only with --env"
    return None

  def skip_build(self):
    return "dry run" if self.args.dry_run else None

  def skip_test_pattern(self):
    if not self.args.test_pattern:
      return "add --test-pattern to check the wiring first"
    if self.args.build_only:
      return "--build-only"
    if self.env == "esp32-s3-ble":
      return "the test pattern is only built for the original ESP32"
    return "dry run" if self.args.dry_run else None

  def skip_flash(self):
    if self.args.build_only:
      return "--build-only"
    return "dry run" if self.args.dry_run else None

  def skip_verify(self):
    if self.args.build_only:
      return "--build-only"
    if self.args.skip_verify:
      return "--skip-verify"
    return "dry run" if self.args.dry_run else None

  # -- progress --
  def overall(self, fraction=0.0):
    weight = self.current.weight if self.current and self.current not in self.skips else 0
    return min(100, int(100 * (self.done_weight + weight * fraction) / self.total_weight))

  def run_tool(self, cmd, parser, label, timeout=None):
    """Runs `cmd`, streaming its output to install.log (and to the screen
    with --verbose) while showing one live status line. Returns
    (exit code, output lines)."""
    self.log.note("$ " + " ".join('"%s"' % c if " " in c else c for c in cmd))
    ui = self.ui
    try:
      proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=tool_env(), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as e:
      raise Abort("Couldn't start %s: %s" % (os.path.basename(cmd[0]), e))

    lines = queue.Queue()

    def reader():
      # Split on \r too: some tools redraw their own progress in place.
      buf = b""
      while True:
        chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
        if not chunk:
          break
        buf += chunk
        parts = re.split(rb"\r\n|\n|\r", buf)
        buf = parts.pop()
        for part in parts:
          lines.put(part.decode("utf-8", errors="replace"))
      if buf:
        lines.put(buf.decode("utf-8", errors="replace"))
      lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    output = []
    started = time.monotonic()
    try:
      while True:
        try:
          line = lines.get(timeout=0.25)
        except queue.Empty:
          line = ""
          if timeout and time.monotonic() - started > timeout:
            proc.kill()
            self.log.note("timed out after %ds" % timeout)
        if line is None:
          break
        if line:
          output.append(line)
          self.log.write(line)
          parser.feed(line)
          if self.args.verbose:
            ui.print("        " + ui.dim(line), log=False)
        parser.tick()
        if not self.args.verbose:
          s = ui.sym
          ui.status("%s %s %s  %s  %s  %s" % (
            label, s["arrow"], ui.bold(parser.phase + s["dots"]),
            ui.dim(fmt_duration(time.monotonic() - started)), ui.dim(s["sep"]),
            ui.dim("%d%% overall" % self.overall(parser.fraction))),
            # Plain (non-terminal) output: a line per phase, digits ignored
            # so "Compiling (12 files)" -> "(13 files)" isn't news.
            key=re.sub(r"\d+", "#", parser.phase))
      proc.wait()
    finally:
      if proc.poll() is None:
        proc.kill()
        proc.wait()
      ui.clear_status()
    self.log.note("exit code %s after %s" % (proc.returncode, fmt_duration(time.monotonic() - started)))
    return proc.returncode, output

  def show_failure_tail(self, count=30):
    ui = self.ui
    ui.print()
    ui.print("      " + ui.dim("Last lines of the log:"))
    for line in self.log.tail(count):
      ui.print("        " + ui.dim(line[: ui.width - 9]), log=False)
    ui.print()
    ui.info("Full details: %s" % self.log.path)

  # =========================================================================
  # 1. Check your computer
  # =========================================================================
  def step_check(self):
    ui = self.ui
    os_name = {"darwin": "macOS " + platform.mac_ver()[0], "win32": "Windows " + platform.release()}.get(
      sys.platform, platform.system() + " " + platform.release())
    ui.ok("%s (%s)" % (os_name.strip(), platform.machine()))
    ui.ok("Python %s" % platform.python_version())
    if PIO_CORE_DIR_MOVED:
      ui.ok("Tools will be stored in %s (PlatformIO can't use a user folder with accents)" % PIO_CORE_DIR)
    if IS_WINDOWS and not REPO_ROOT.isascii():
      ui.warn("This folder's path (%s) has accents or other special letters, which often makes the "
              "build fail on Windows. If it does, move the folder somewhere like C:\\tesla-panel and "
              "run the installer from there." % REPO_ROOT)

    # Disk space: the toolchain is the big one, and it lives in the user's
    # home (~/.platformio), not in this folder.
    need = DISK_NEEDED_AFTER if self.toolchain_present() else DISK_NEEDED_FIRST_RUN
    probe = PIO_CORE_DIR if os.path.isdir(PIO_CORE_DIR) else os.path.expanduser("~")
    free = shutil.disk_usage(probe).free
    if free >= need:
      ui.ok("Disk space: %.1f GB free (needs about %.1f GB)" % (free / 1e9, need / 1e9))
    else:
      ui.warn("Only %.1f GB free on the disk holding %s; the tools need about %.1f GB." % (
        free / 1e9, PIO_CORE_DIR, need / 1e9))
      if not ui.ask_yes_no("Continue anyway?", default=False):
        raise Abort("Not enough disk space.", "Free up some space and run the installer again.")

    # Internet: only a warning, proxies/firewalls can make this check lie.
    if self.venv_ready_marker() and self.toolchain_present():
      ui.ok("Tools already downloaded, internet is only needed if something is missing")
      return
    hosts = ["pypi.org", "github.com", "api.registry.platformio.org"]
    failed = []

    def probe_host(host):
      try:
        socket.create_connection((host, 443), timeout=4).close()
      except OSError:
        failed.append(host)

    threads = [threading.Thread(target=probe_host, args=(h,), daemon=True) for h in hosts]
    for t in threads:
      t.start()
    for t in threads:
      t.join(5)
    if failed:
      ui.warn("Couldn't reach %s. The first run downloads tools from the internet - "
              "if you're offline or behind a strict firewall, the next steps may fail." % ", ".join(failed))
    else:
      ui.ok("Internet connection works")

  # =========================================================================
  # 2. Set up PlatformIO
  # =========================================================================
  def venv_works(self):
    """True when the venv has everything we need. Logs why not."""
    if not os.path.exists(self.venv_python):
      return False
    try:
      out = subprocess.run(
        [self.venv_python, "-c",
         "import platformio, esptool, serial; print(platformio.__version__, esptool.__version__)"],
        capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
      self.log.note("venv check failed: %s" % e)
      return False
    self.log.note("venv check: rc=%d %s %s" % (out.returncode, out.stdout.strip(), out.stderr.strip()[-500:]))
    if out.returncode != 0:
      return False
    versions = out.stdout.split()
    return len(versions) == 2 and versions[0].startswith("6.") and versions[1].startswith("4.")

  def venv_pip_works(self):
    """True when .venv has a Python that runs and has pip."""
    if not os.path.exists(self.venv_python):
      return False
    try:
      out = subprocess.run([self.venv_python, "-m", "pip", "--version"], capture_output=True, text=True,
                           timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
      self.log.note("venv pip check failed: %s" % e)
      return False
    self.log.note("venv pip check: rc=%d %s" % (out.returncode, (out.stdout + out.stderr).strip()[-300:]))
    return out.returncode == 0

  def remove_venv(self, quiet=False):
    self.log.note("removing venv")

    def make_writable(func, path, _):
      # Windows refuses to delete read-only files.
      os.chmod(path, 0o700)
      func(path)

    try:
      shutil.rmtree(VENV_DIR, onerror=make_writable)
    except OSError as e:
      if quiet:
        return
      raise Abort("Couldn't delete the broken .venv folder: %s" % e,
                  "Close other installer windows or editors using it, delete %s by hand, and run the "
                  "installer again." % VENV_DIR)

  def step_setup(self):
    ui = self.ui
    if self.venv_works():
      ui.ok("PlatformIO is already installed in %s" % relpath(VENV_DIR))
      return

    ui.info("PlatformIO is the tool that downloads the compiler and builds the firmware. "
            "It is installed privately in the .venv folder here, so it doesn't change anything "
            "else on your computer (delete .venv to remove it).")

    if os.path.isdir(VENV_DIR) and not self.venv_pip_works():
      # Half-made (Ctrl-C, or venv failing on Debian without python3-venv),
      # from another OS, or its Python got uninstalled - rebuild it.
      if not os.path.exists(os.path.join(VENV_DIR, "pyvenv.cfg")):
        raise Abort("There is a .venv folder here that isn't a Python environment.",
                    "Move or delete %s, then run the installer again." % VENV_DIR)
      ui.info("The existing .venv folder is incomplete (an earlier run was probably interrupted); "
              "recreating it.")
      self.remove_venv()

    self.create_venv()

    for attempt in (1, 2):
      code, _ = self.run_tool(
        [self.venv_python, "-m", "pip", "install", "--disable-pip-version-check"] + PIP_PACKAGES,
        PipParser(), "PlatformIO")
      if code == 0 and self.venv_works():
        break
      if attempt == 1:
        ui.warn("Installing PlatformIO failed, trying once more in a fresh .venv" + ui.sym["dots"])
        # pip trusts half-installed packages from an interrupted run
        # ("already satisfied"), so start over; downloads are cached.
        self.remove_venv()
        time.sleep(3)
        self.create_venv()
    else:
      self.show_failure_tail()
      raise Abort("Couldn't install PlatformIO.",
                  "Check your internet connection (a proxy may need HTTPS_PROXY set) and run the installer again.")
    ui.ok("PlatformIO installed")

  def create_venv(self):
    ui = self.ui
    if not os.path.isdir(VENV_DIR):
      ui.status("Creating the Python environment" + ui.sym["dots"])
      result = subprocess.run([sys.executable, "-m", "venv", VENV_DIR], capture_output=True, text=True)
      output = result.stdout + result.stderr
      self.log.write(output)
      if result.returncode != 0 or not self.venv_pip_works():
        ui.clear_status()
        # Don't leave a half-made venv behind for the next run.
        self.remove_venv(quiet=True)
        hint = "Details are in %s." % self.log.path
        if "ensurepip" in output and shutil.which("apt-get"):
          # Debian/Ubuntu ship Python's venv module as a separate package.
          hint = ("Python's venv module is missing - Debian and Ubuntu package it separately. "
                  "Install it with:  sudo apt install python%d.%d-venv  then run the installer again."
                  % sys.version_info[:2])
        elif "ensurepip" in output:
          hint = "Python's venv/ensurepip module is missing: install it with your package manager."
        raise Abort("Couldn't create the Python environment in .venv.", hint)
      ui.clear_status()

  # =========================================================================
  # 3. Find the board
  # =========================================================================
  def helper(self, *args):
    """Runs this file's serial helper inside the venv (it needs pyserial)."""
    return [self.venv_python, os.path.abspath(__file__), "--serial-helper"] + list(args)

  def list_ports(self):
    try:
      result = subprocess.run(self.helper("list-ports"), capture_output=True, text=True, timeout=30)
    except subprocess.SubprocessError as e:
      raise Abort("Couldn't list the serial ports: %s" % e)
    self.log.note("ports: " + result.stdout.strip() + result.stderr.strip()[-500:])
    if result.returncode != 0:
      raise Abort("Couldn't list the serial ports.", "Details are in %s." % self.log.path)
    ports = json.loads(result.stdout)
    # Only USB devices: this drops Bluetooth ports, debug consoles and
    # motherboard COM/ttyS ports, which never have a USB vendor ID.
    usb = [p for p in ports if p.get("vid") is not None]
    for p in usb:
      p["known"] = KNOWN_USB_VIDS.get(p["vid"])
    usb.sort(key=lambda p: (p["known"] is None, p["device"]))
    return usb

  @staticmethod
  def describe_port(p):
    kind = p["known"] or "unknown USB serial device"
    desc = p.get("description") or ""
    if desc and desc != "n/a" and desc != p["device"]:
      return "%s  (%s - %s)" % (p["device"], desc, kind)
    return "%s  (%s)" % (p["device"], kind)

  def no_board_help(self):
    ui = self.ui
    ui.warn("No ESP32 board found on USB.")
    tips = [
      "Plug the ESP32 into this computer with a USB cable. Many cables only charge: if nothing shows up, "
      "try another cable (one that you know transfers data, e.g. for a phone).",
      "Try another USB port, and avoid USB hubs if you can.",
    ]
    if IS_MAC:
      tips.append("Recent macOS versions include the usual drivers. If your board has a CH340/CH9102 chip "
                  "and still doesn't appear, install the driver: %s" % DRIVER_LINKS["CH340/CH9102 (macOS)"])
    elif IS_WINDOWS:
      tips.append("Windows may need a driver. Open Device Manager: an 'Unknown device' or a device with a "
                  "yellow mark means the driver is missing. CP210x: %s  -  CH340/CH9102: %s" % (
                    DRIVER_LINKS["CP210x"], DRIVER_LINKS["CH340/CH9102 (Windows)"]))
    elif IS_LINUX:
      tips.append("On Ubuntu, the 'brltty' package can grab CH340 boards: if the board appears and "
                  "disappears, run  sudo apt remove brltty  and re-plug it.")
    ui.bullets(tips)

  def check_port_access(self, device):
    """Linux: serial ports belong to a group (dialout, uucp) you must be in."""
    if not IS_LINUX or not os.path.exists(device) or os.access(device, os.R_OK | os.W_OK):
      return
    try:
      import grp
      group = grp.getgrgid(os.stat(device).st_gid).gr_name
    except (ImportError, KeyError, OSError):
      group = "dialout"
    raise Abort("You don't have permission to use %s." % device,
                "Add yourself to the '%s' group:  sudo usermod -aG %s $USER  then log out and back in "
                "(or restart), and run the installer again." % (group, group))

  def step_find_board(self):
    ui = self.ui
    if self.port:
      if not IS_WINDOWS and not os.path.exists(self.port):
        raise Abort("The port %s given with --port doesn't exist." % self.port,
                    "Run the installer without --port to list the boards it can see.")
      ui.ok("Using the port you gave: %s" % self.port)
    else:
      while True:
        ports = self.list_ports()
        if ports:
          break
        self.no_board_help()
        if not ui.interactive:
          raise Abort("No board found.", "Plug in the board and run the installer again.")
        ui.pause("Plug the board in, then press Enter to look again (Ctrl-C to stop)")

      known = [p for p in ports if p["known"]]
      if len(ports) == 1:
        chosen = ports[0]
      elif not ui.interactive:
        if len(known) != 1:
          raise Abort("Found several USB serial devices and can't tell which one is the ESP32.",
                      "Unplug the others, or pick one with --port. Found: " +
                      ", ".join(p["device"] for p in ports))
        chosen = known[0]
      else:
        ui.info("Several USB serial devices are connected. Which one is the ESP32? "
                "(Not sure? Unplug the board, run the installer again and see which one disappears.)")
        chosen = ports[ui.choose("Board", [self.describe_port(p) for p in ports])]
      self.port = chosen["device"]
      self.port_info = chosen
      ui.ok("Found: %s" % self.describe_port(chosen))
      if not chosen["known"]:
        ui.warn("This doesn't look like a usual ESP32 USB chip, but let's try it.")

    if self.args.dry_run:
      if self.env:
        self.explain_env()
      else:
        ui.info("Dry run: not connecting to the board, so the chip type (ESP32 or ESP32-S3) isn't detected.")
      return

    self.check_port_access(self.port)
    if self.env:
      ui.ok("Firmware: %s (chosen with --env)" % self.env)
    else:
      self.detect_chip()
    self.explain_env()

  def detect_chip(self):
    ui = self.ui
    while True:
      code, output = self.run_tool(
        [self.venv_python, "-m", "esptool", "--port", self.port, "chip_id"],
        EsptoolParser(), "Chip", timeout=90)
      text = "\n".join(output)
      match = (re.search(r"Chip is (ESP32[\w-]*)", text)
               or re.search(r"Detecting chip type\.*\s*(ESP32[\w-]*)", text))
      if code == 0 and match:
        break
      self.explain_connection_failure(text)
      if not ui.ask_yes_no("Try again?", default=True, noninteractive=False):
        raise Abort("Couldn't identify the board.",
                    "If you know the chip, you can skip detection with --env esp32-classic (ESP32) or "
                    "--env esp32-s3-ble (ESP32-S3).")
    self.chip = match.group(1)
    mac = re.search(r"MAC:\s*([0-9a-fA-F:]{17})", text)
    self.chip_mac = mac.group(1).upper() if mac else None
    chip = self.chip.upper()
    if chip.startswith("ESP32-S3"):
      self.env = "esp32-s3-ble"
    elif re.match(r"ESP32($|-D|-U|-S0|-PICO)", chip):  # the original ESP32's package variants
      self.env = "esp32-classic"
    else:
      raise Abort("This board has an %s chip, which this firmware doesn't support." % self.chip,
                  "Supported: the original ESP32 (e.g. ESP32-DevKitC V4) and the ESP32-S3 "
                  "(ESP32-S3-DevKitC-1). The panel driver needs one of those two.")
    ui.ok("Chip: %s" % self.chip)

  def explain_env(self):
    info = ENVS[self.env]
    ui = self.ui
    ui.ok("Firmware: %s - uses %s, shows up as \"%s\"" % (self.env, info["radio"], info["device_name"]))
    ui.info("In the TeslaLED app you will pick the \"%s\" connection." % info["app_label"])
    if not info["tested"]:
      ui.warn("The %s firmware builds, but hasn't been tested on a real board yet. "
              "If it misbehaves, please report it." % info["chip"])

  def explain_connection_failure(self, text):
    ui = self.ui
    lowered = text.lower()
    if any(s in lowered for s in ("resource busy", "could not open port", "access is denied", "port is busy",
                                  "permissionerror", "permission denied", "being used by another")):
      ui.error("The port %s is busy or not accessible." % self.port)
      ui.bullets(["Another program is probably using it: close the Arduino IDE, any serial monitor "
                  "(PlatformIO/VS Code, screen, PuTTY...) and other installer windows, then try again."])
      if IS_LINUX:
        self.check_port_access(self.port)
    elif any(s in lowered for s in ("failed to connect", "wrong boot mode", "no serial data received",
                                    "timed out waiting for packet", "serial data stream stopped")):
      ui.error("Couldn't talk to the board's bootloader.")
      ui.bullets([
        "Hold the BOOT button on the ESP32 (sometimes labelled IO0), then answer 'y' below and keep "
        "holding it until you see 'Writing' or 'Chip' (a few seconds), then let go.",
        "If that doesn't help: unplug and re-plug the USB cable, or try another cable/port.",
      ])
    else:
      ui.error("The board didn't respond as expected.")
      self.show_failure_tail(15)

  # =========================================================================
  # 4. Build the firmware
  # =========================================================================
  def build(self, env, label):
    ui = self.ui
    for attempt in range(1, 4):
      parser = BuildParser()
      started = time.monotonic()
      code, output = self.run_tool(self.pio("run", "-e", env), parser, label)
      if code == 0:
        usage = ", ".join("%s %s%%" % (k, v) for k, v in sorted(parser.usage.items(), reverse=True))
        ui.ok("Built %s in %s%s" % (env, fmt_duration(time.monotonic() - started),
                                    " (uses %s)" % usage if usage else ""))
        return
      text = "\n".join(output[-200:])
      # Another PlatformIO run (a second installer, an IDE) holding the
      # package lock - worth waiting for rather than failing.
      if re.search(r"LockFile|lock.*(timeout|timed out)|Could not (acquire|lock)", text, re.I) and attempt < 3:
        ui.warn("PlatformIO is busy (another build is running?), retrying in 15s" + ui.sym["dots"])
        time.sleep(15)
        continue
      break
    self.show_failure_tail()
    hint = "Run the installer again - a failed download is often just a network hiccup."
    if re.search(r"No space left|Errno 28", text):
      hint = "The disk is full. Free up some space and run the installer again."
    elif re.search(r"HTTPClientError|ConnectionError|Could not resolve|timed out|SSL", text, re.I):
      hint = "It looks like a download failed. Check your internet connection and run the installer again."
    raise Abort("The firmware build failed.", hint)

  def step_build(self):
    if self.toolchain_present():
      self.ui.info("Compiling the firmware for your board.")
    else:
      self.ui.info("Compiling the firmware for your board. This first time, PlatformIO also downloads "
                   "the compiler and libraries (about 1 GB) - that's the slow part and happens only once.")
    self.build(self.env, "Build")

  # =========================================================================
  # 5. Wiring check
  # =========================================================================
  def step_test_pattern(self):
    ui = self.ui
    ui.info("This puts a simple test image on the panel to check the wiring before the real firmware. "
            "Make sure the panel is connected and powered.")
    self.build(TEST_PATTERN_ENV, "Test pattern build")
    self.flash(TEST_PATTERN_ENV, "Test pattern flash")
    ui.print()
    ui.info("Look at the panel now. You should see:")
    ui.bullets(["a thin white border around the whole panel",
                "on the left half: thin white vertical lines",
                "on the right half: three rows of small dots - red, then green, then blue"])
    if not ui.interactive:
      ui.info("(%s: not waiting for an answer - check it yourself.)" % ui.why_not)
      return
    if ui.ask_yes_no("Do you see that pattern?", default=True):
      ui.ok("Wiring looks good")
      return
    ui.warn("Things to check (unplug everything first):")
    ui.bullets([
      "Power: the panel needs its own 5V supply of at least 4A, plugged into the adapter board's USB-C or DC "
      "jack. The ESP32's USB cable alone can't power the panel.",
      "Ribbon cable: it must go into the panel's INPUT connector (the arrows printed on the back of the panel "
      "point away from it), and into the adapter's HUB75 port. Check it isn't upside down.",
      "Adapter revision: this firmware expects the seengreat adapter rev 2.x (printed on the board). "
      "Rev 1.x uses different pins.",
      "The ESP32 must be fully seated in the adapter's socket, the right way round.",
      "A blank panel with the ESP32 LED on usually means no panel power; garbled colours usually mean a "
      "loose ribbon.",
    ])
    if not ui.ask_yes_no("Flash the real firmware anyway?", default=False):
      raise Abort("Stopped after the wiring check.",
                  "Fix the wiring, then run the installer again with --test-pattern to re-check.")

  # =========================================================================
  # 6. Flash
  # =========================================================================
  def flash(self, env, label):
    ui = self.ui
    self.check_port_access(self.port)
    while True:
      started = time.monotonic()
      code, output = self.run_tool(self.pio("run", "-e", env, "-t", "upload", "--upload-port", self.port),
                                   UploadParser(), label)
      if code == 0:
        ui.ok("Flashed %s in %s" % (env, fmt_duration(time.monotonic() - started)))
        return
      self.explain_connection_failure("\n".join(output[-300:]))
      if not ui.ask_yes_no("Try flashing again?", default=True, noninteractive=False):
        raise Abort("Flashing failed.", "Details are in %s." % self.log.path)

  def step_flash(self):
    self.ui.info("Writing the firmware to the board over USB. Don't unplug it until this is done.")
    self.flash(self.env, "Flash")

  # =========================================================================
  # 7. Check that it started
  # =========================================================================
  def step_verify(self):
    ui = self.ui
    ui.info("Restarting the board and reading its start-up messages" + ui.sym["dots"])
    cmd = self.helper("read-boot", self.port, str(VERIFY_TIMEOUT))
    self.log.note("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", env=tool_env())
    lines, error, ready = [], None, False
    started = time.monotonic()
    events = queue.Queue()

    def reader():
      for event_line in proc.stdout:
        events.put(event_line)
      events.put(None)

    threading.Thread(target=reader, daemon=True).start()
    try:
      while True:
        try:
          raw = events.get(timeout=0.25)
        except queue.Empty:
          raw = ""
        if raw is None:
          break
        if raw:
          try:
            event = json.loads(raw)
          except ValueError:
            event = {"note": "helper: " + raw.strip()}
          if "error" in event:
            error = event["error"]
          elif "note" in event:
            self.log.note(event["note"])
          elif "line" in event:
            line = event["line"]
            lines.append(line)
            self.log.write("board> " + line)
            if self.args.verbose:
              ui.print("        " + ui.dim(line), log=False)
            address = ADDRESS_RE.search(line)
            if address:
              self.address = address.group(1).upper()
            if READY_RE.search(line):
              ready = True
              break
        elapsed = time.monotonic() - started
        if elapsed > VERIFY_TIMEOUT + 15:
          # The helper stops by itself; this only guards a hung port open.
          error = error or "no answer from %s" % self.port
          break
        ui.status("Waiting for the board to start%s %s  %s  %s  %s" % (
          ui.sym["dots"],
          ui.dim("(%d lines received)" % len(lines)),
          ui.dim("%s / %ds" % (fmt_duration(elapsed), VERIFY_TIMEOUT)), ui.dim(ui.sym["sep"]),
          ui.dim("%d%% overall" % self.overall(min(elapsed / VERIFY_TIMEOUT, 0.99)))),
          key="waiting")
    finally:
      if proc.poll() is None:
        proc.terminate()
        try:
          proc.wait(5)
        except subprocess.TimeoutExpired:
          proc.kill()
      ui.clear_status()
    stderr = proc.stderr.read() if proc.stderr else ""
    if stderr.strip():
      self.log.write(stderr)
      if proc.returncode and not error:
        error = stderr.strip().splitlines()[-1]

    if ready:
      ui.ok("The board started and reported \"Ready\"")
      if self.address:
        ui.ok("Bluetooth address: %s" % ui.bold(ui.green(self.address)))
      else:
        ui.warn("The board didn't print its Bluetooth address.")
      return
    if error:
      ui.warn("Couldn't read from the board: %s" % error)
      if "busy" in error.lower() or "denied" in error.lower():
        ui.info("Close any serial monitor / Arduino IDE that may be using %s." % self.port)
    else:
      ui.warn("The board didn't say \"Ready\" within %ds." % VERIFY_TIMEOUT)
    if self.env == "esp32-classic":
      seen = "the board should appear in your phone's Bluetooth list as \"%s\"" % ENVS[self.env]["device_name"]
    else:
      seen = ("the TeslaLED app should be able to connect to it (BLE boards usually don't show up in the "
              "phone's Bluetooth settings - that's normal)")
    ui.info("That doesn't necessarily mean it failed - flashing succeeded. Try unplugging the board and "
            "plugging it back in; the panel should light up and %s." % seen)
    if self.port_info and self.port_info.get("vid") == 0x303A:
      ui.info("Your board is plugged in through the ESP32-S3's native USB port; its messages come out on "
              "the other USB port (labelled UART or COM). Use that one to see them.")
    if lines:
      ui.print("      " + ui.dim("What the board printed:"))
      for line in lines[-15:]:
        ui.print("        " + ui.dim(line[: ui.width - 9]))

  # =========================================================================
  # Plan, next steps, main loop
  # =========================================================================
  def print_plan(self):
    ui = self.ui
    ui.print()
    ui.print(ui.bold("Tesla LED panel installer"))
    ui.print("Here is what will happen:")
    ui.print()
    for i, step in enumerate(self.steps, 1):
      reason = self.skips.get(step)
      title = "%d. %s" % (i, step.title)
      if reason:
        ui.print("  " + ui.dim("%-34s skipped (%s)" % (title, reason)))
      else:
        ui.print("  %-34s %s" % (title, ui.dim(step.estimate(self))))
    ui.print()
    if not (self.venv_ready_marker() and self.toolchain_present()):
      ui.info("The first run is the slow one (10-15 min on a typical connection): it downloads about "
              "1 GB of compiler tools. Running it again later takes about a minute.", indent=0)
    ui.info("Everything is also written to %s, in case something goes wrong." % relpath(self.log.path), indent=0)
    ui.info("You can stop at any time with Ctrl-C; running it again is safe.", indent=0)
    if not (self.args.build_only or self.args.dry_run):
      ui.print()
      ui.info("Before you start: plug the ESP32 into this computer with a USB cable. To see the panel "
              "light up at the end, connect it to the adapter and power it too.", indent=0)

  def print_dry_run(self):
    ui = self.ui
    env = self.env or "<detected: esp32-classic or esp32-s3-ble>"
    port = self.port or "<port>"

    def show(*pio_args):
      ui.print("  " + " ".join([relpath(self.venv_python)] + self.pio(*pio_args)[1:]))

    ui.print()
    ui.print(ui.bold("Dry run - this is what would run next:"))
    if self.args.test_pattern and not self.args.build_only and self.env != "esp32-s3-ble":
      show("run", "-e", TEST_PATTERN_ENV, "-t", "upload", "--upload-port", port)
    show("run", "-e", env)
    if not self.args.build_only:
      show("run", "-e", env, "-t", "upload", "--upload-port", port)
      if not self.args.skip_verify:
        ui.print("  (then restart the board and read its messages on %s at %d baud)" % (port, BAUD))

  def print_next_steps(self):
    ui = self.ui
    info = ENVS[self.env]
    address = self.address
    guessed = False
    if not address and self.chip_mac:
      # The Bluetooth MAC is the chip's base MAC + 2 on both ESP32 and S3.
      value = (int(self.chip_mac.replace(":", ""), 16) + 2) & 0xFFFFFFFFFFFF
      address = ":".join("%02X" % ((value >> s) & 0xFF) for s in range(40, -1, -8))
      guessed = True
    shown = address or "(the address your phone shows for %s)" % info["device_name"]
    lines = []
    if self.env == "esp32-classic":
      lines += [
        "1. Power the panel. On your phone, open Settings > Bluetooth and pair with \"%s\"." % info["device_name"],
        "   Android may then say \"Can't connect\" - that's normal: the panel isn't a headset, "
        "the app connects to it by itself.",
        "2. In the TeslaLED app, tap the connection button in the top bar (it shows PI, ESP32 or BLE), "
        "choose \"%s\" and enter this address:" % info["app_label"],
      ]
    else:
      lines += [
        "1. Power the panel. No Bluetooth pairing needed with BLE - don't pair it in the phone settings.",
        "2. In the TeslaLED app, tap the connection button in the top bar (it shows PI, ESP32 or BLE), "
        "choose \"%s\" and enter this address:" % info["app_label"],
      ]
    lines.append("      " + shown)
    if guessed:
      lines.append("   (worked out from the chip's ID because the board didn't report it - if it doesn't "
                   "connect, check the address in your phone's Bluetooth list)")
    lines += ["3. Type a message in the app - it shows up on the panel."]
    ui.print()
    ui.box("Next steps", lines)

  def run(self):
    for step in self.steps:
      reason = step.skip_reason(self)
      if reason:
        self.skips[step] = reason
    # A dry run still shows the percentages a real run would have.
    self.total_weight = float(sum(s.weight for s in self.steps if self.skips.get(s) in (None, "dry run"))) or 1.0
    self.log.note("tesla-panel installer, args=%s, python=%s, platform=%s" % (
      sys.argv[1:], sys.version.split()[0], platform.platform()))
    self.print_plan()
    if self.ui.interactive and not self.args.dry_run:
      self.ui.print()
      self.ui.pause("Press Enter to start (Ctrl-C to cancel)")

    count = len(self.steps)
    for i, step in enumerate(self.steps, 1):
      self.current = step
      # Re-evaluate: an earlier step (board detection) can change the answer.
      reason = self.skips.get(step) or step.skip_reason(self)
      header = "[%d/%d] %s" % (i, count, step.title)
      self.ui.print()
      if reason:
        self.ui.print(self.ui.dim("%s  - skipped (%s)" % (header, reason)))
        continue
      self.ui.print("%s  %s" % (self.ui.bold(self.ui.cyan(header)), self.ui.dim("~%d%% overall %s %s" % (
        self.overall(), self.ui.sym["sep"], step.estimate(self)))))
      self.log.note("step %d: %s" % (i, step.title))
      step.run(self)
      self.done_weight += step.weight

    elapsed = fmt_duration(time.monotonic() - self.started)
    ui = self.ui
    ui.print()
    if self.args.dry_run:
      self.print_dry_run()
      ui.print()
      ui.print(ui.green("Dry run finished in %s - nothing was built or flashed." % elapsed))
      return
    if self.args.build_only:
      firmware = relpath(os.path.join(REPO_ROOT, ".pio", "build", self.env, "firmware.bin"))
      ui.print(ui.green(ui.bold("%s Build finished in %s." % (ui.sym["ok"], elapsed))))
      ui.info("Firmware: %s" % firmware, indent=0)
      ui.info("Run the installer without --build-only to flash it.", indent=0)
      return
    ui.print(ui.green(ui.bold("%s All done in %s." % (ui.sym["ok"], elapsed))))
    self.print_next_steps()
    self.log.note("summary: env=%s port=%s chip=%s address=%s elapsed=%s" % (
      self.env, self.port, self.chip, self.address, elapsed))


# ---------------------------------------------------------------------------
# Serial helper (runs inside the venv, where pyserial is available)
# ---------------------------------------------------------------------------

def serial_helper(argv):
  """Tiny subcommands that need pyserial, printing JSON on stdout:
    list-ports              -> one JSON list of ports
    read-boot PORT TIMEOUT  -> one JSON object per line the board prints
  """
  import serial
  from serial.tools import list_ports

  if argv[0] == "list-ports":
    print(json.dumps([{
      "device": p.device, "description": p.description, "hwid": p.hwid,
      "vid": p.vid, "pid": p.pid, "manufacturer": p.manufacturer, "serial": p.serial_number,
    } for p in list_ports.comports()]))
    return 0

  if argv[0] == "read-boot":
    port, timeout = argv[1], float(argv[2])
    serial_errors = (serial.SerialException, OSError, ValueError)

    def emit(obj):
      print(json.dumps(obj), flush=True)

    def open_port():
      ser = serial.Serial()
      ser.port, ser.baudrate, ser.timeout = port, BAUD, 0.2
      # Set both lines released *before* opening, so opening doesn't glitch
      # the board into its bootloader (DTR drives IO0 on dev boards).
      ser.dtr = False
      ser.rts = False
      ser.open()
      return ser

    deadline = time.monotonic() + timeout
    try:
      ser = open_port()
    except serial_errors as e:
      emit({"error": str(e)})
      return 2
    # Reset: RTS drives EN (reset) low; DTR/IO0 stays high = normal boot.
    try:
      ser.rts = True
      ser.dtr = False  # Windows' usbser driver only applies RTS on a DTR write (same trick as esptool)
      time.sleep(0.15)
      ser.rts = False
      ser.dtr = False
    except (serial.SerialException, OSError) as e:
      # Some adapters can't do it - still listen, the user can re-plug.
      emit({"note": "couldn't reset the board: %s" % e})
    buf = b""
    while time.monotonic() < deadline:
      try:
        chunk = ser.read(256)
      except (serial.SerialException, OSError):
        # Native USB ports disappear for a moment on reset - reopen.
        try:
          ser.close()
        except Exception:
          pass
        time.sleep(0.5)
        try:
          ser = open_port()
        except (serial.SerialException, OSError):
          pass
        continue
      buf += chunk
      while b"\n" in buf:
        raw, buf = buf.split(b"\n", 1)
        # The ROM prints at 74880 baud first, which reads as noise here;
        # the firmware only prints ASCII, so keep just that.
        line = "".join(ch for ch in raw.decode("utf-8", errors="replace") if 32 <= ord(ch) < 127).strip()
        if line:
          emit({"line": line})
    try:
      ser.close()
    except Exception:
      pass
    return 0

  return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv):
  parser = argparse.ArgumentParser(
    prog="install",
    description="Builds the Tesla LED panel firmware and flashes it to an ESP32 over USB,\n"
                "explaining each step. Just run it without options for the guided install.",
    epilog="Examples:\n"
           "  ./install.sh                      guided install (Windows: .\\install.ps1)\n"
           "  ./install.sh --test-pattern       check the panel wiring first\n"
           "  ./install.sh --dry-run            show the plan and the boards found, flash nothing\n"
           "  ./install.sh --env esp32-classic --build-only\n"
           "                                    only build (no board needed)\n"
           "\nThe full log of the last run is in install.log.",
    formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--env", choices=sorted(ENVS),
                      help="which firmware to use instead of detecting it from the chip: esp32-classic "
                           "(ESP32, Bluetooth Classic) or esp32-s3-ble (ESP32-S3, BLE)")
  parser.add_argument("--port", help="serial port of the board (e.g. /dev/ttyUSB0, COM3) instead of detecting it")
  parser.add_argument("--test-pattern", action="store_true",
                      help="first flash a test image to check the panel wiring, then the real firmware "
                           "(original ESP32 only)")
  parser.add_argument("-y", "--yes", action="store_true",
                      help="never ask: pick the only board found, fail if a choice is ambiguous")
  parser.add_argument("--verbose", action="store_true", help="also show the full PlatformIO output")
  parser.add_argument("--build-only", action="store_true", help="build the firmware without flashing it")
  parser.add_argument("--skip-verify", action="store_true",
                      help="don't read the board's start-up messages after flashing")
  parser.add_argument("--dry-run", action="store_true",
                      help="show the plan and the boards found, without building, flashing or opening the "
                           "port (PlatformIO still gets installed in .venv, to list the boards)")
  parser.add_argument("--serial-helper", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
  return parser.parse_args(argv)


def main(argv=None):
  # Text from outside the installer (board output, tool output, paths) can
  # hold characters a cp1252 pipe can't encode: replace them, don't crash.
  for stream in (sys.stdout, sys.stderr):
    try:
      stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
      pass
  args = parse_args(sys.argv[1:] if argv is None else argv)
  if args.serial_helper:
    return serial_helper(args.serial_helper)

  installer = None
  try:
    installer = Installer(args)
    installer.run()
    return 0
  except KeyboardInterrupt:
    if installer:
      installer.ui.clear_status()
      installer.log.note("interrupted (Ctrl-C)")
    print("\n\nStopped (Ctrl-C). Nothing is broken - run the installer again whenever you're ready.")
    return 130
  except Abort as e:
    ui = installer.ui
    ui.print()
    ui.print(ui.red(ui.bold("%s %s" % (ui.sym["fail"], e))))
    if e.hint:
      ui.info(e.hint, indent=2)
    installer.log.note("aborted: %s | %s" % (e, e.hint))
    return 1
  except Exception:
    if installer is None:
      raise
    installer.ui.clear_status()
    installer.log.write(traceback.format_exc())
    print("\n%s Something unexpected went wrong. Details are in %s" % (installer.ui.sym["fail"], installer.log.path))
    print("  Running the installer again often helps; if it keeps happening, please share that file.")
    return 1


if __name__ == "__main__":
  sys.exit(main())
