#!/usr/bin/env bash
# Tesla LED panel installer - macOS and Linux.
#
#   ./install.sh            guided install: builds the firmware and flashes
#                           it to the ESP32 plugged in over USB
#   ./install.sh --help     all options
#
# This script only makes sure Python 3.9+ is available (offering to install
# it if not), then hands over to tools/install.py, which does the real work
# and takes the same options on every OS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$SCRIPT_DIR/tools/install.py"

# --yes means "never ask": we then print the command instead of running it.
NONINTERACTIVE=0
for arg in "$@"; do
  case "$arg" in -y|--yes) NONINTERACTIVE=1 ;; esac
done
[ -t 0 ] || NONINTERACTIVE=1

say() { printf '%s\n' "$*"; }

ask() {  # ask "question" -> 0 for yes (default no)
  [ "$NONINTERACTIVE" = 1 ] && return 1
  local reply
  read -r -p "  ? $1 [y/N] " reply || return 1
  case "$reply" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

# Prints the first Python >= 3.9 found, or nothing.
find_python() {
  local candidate
  for candidate in python3 python; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    # On macOS without the developer tools, /usr/bin/python3 is only a stub
    # that pops up an install dialog - don't count it (or trigger it).
    if [ "$(uname -s)" = Darwin ] && [ "$(command -v "$candidate")" = /usr/bin/python3 ] \
        && ! xcode-select -p >/dev/null 2>&1; then
      continue
    fi
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      command -v "$candidate"
      return
    fi
  done
}

# Runs "$@" after asking, or prints it for the user to run.
offer() {
  local why="$1"; shift
  say ""
  say "  $why"
  say "  The command is:  $*"
  if ask "Run it now?"; then
    if ! "$@"; then
      say ""
      say "  That command failed (see the messages above). Fix the problem or run it"
      say "  yourself, then start ./install.sh again."
      exit 1
    fi
  else
    say ""
    say "  Run that command yourself, then start ./install.sh again."
    exit 1
  fi
}

install_python() {
  say ""
  say "This installer needs Python 3.9 or newer, and none was found."
  case "$(uname -s)" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        offer "Python can be installed with Homebrew (takes 1-2 minutes)." brew install python@3.12
      else
        say ""
        say "  Install Python in one of these ways, then run ./install.sh again:"
        say "    - Download the macOS installer from https://www.python.org/downloads/"
        say "    - Or install Apple's developer tools (includes Python 3):  xcode-select --install"
        exit 1
      fi
      ;;
    Linux)
      if command -v apt-get >/dev/null 2>&1; then
        offer "Python can be installed with apt (asks for your password)." \
          sudo sh -c "apt-get update && apt-get install -y python3 python3-venv"
      elif command -v dnf >/dev/null 2>&1; then
        offer "Python can be installed with dnf (asks for your password)." sudo dnf install -y python3
      elif command -v pacman >/dev/null 2>&1; then
        offer "Python can be installed with pacman (asks for your password)." sudo pacman -S --needed python
      elif command -v zypper >/dev/null 2>&1; then
        offer "Python can be installed with zypper (asks for your password)." sudo zypper install -y python3
      else
        say "  Install Python 3.9+ with your distribution's package manager, then run ./install.sh again."
        exit 1
      fi
      ;;
    *)
      say "  Install Python 3.9+ from https://www.python.org/downloads/ then run ./install.sh again."
      exit 1
      ;;
  esac
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  install_python
  hash -r
  PYTHON="$(find_python || true)"
  if [ -z "$PYTHON" ]; then
    say ""
    say "Python still isn't found. Open a new terminal window and run ./install.sh again."
    exit 1
  fi
fi

# Debian/Ubuntu ship Python without its venv module (needed for the private
# PlatformIO install in .venv).
if ! "$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
  version="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  offer "Python's venv module is missing (Debian/Ubuntu package it separately)." \
    sudo apt-get install -y "python$version-venv"
fi

exec "$PYTHON" "$INSTALLER" "$@"
