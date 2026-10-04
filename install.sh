#!/usr/bin/env bash
# Jarvis one-step install + launch for macOS and Linux.
#   ./install.sh              install (first time) and start
#   ./install.sh --reinstall  wipe the environment and install again
#   ./install.sh --headless   (and any other flags) passed on to Jarvis
# Double-click instead: Jarvis.desktop (Linux) or Jarvis.command (macOS) in this folder.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
MARKER="$VENV/.jarvis-installed"
OS="$(uname)"

if [[ "${1:-}" != "--reinstall" && -f "$MARKER" && -x "$VENV/bin/python" ]]; then
  exec "$VENV/bin/python" -m jarvis "$@"
fi
REINSTALL=0
if [[ "${1:-}" == "--reinstall" ]]; then REINSTALL=1; shift; fi

fail() {
  echo
  echo "  ✗ $1"
  [[ -n "${2:-}" ]] && echo "    $2"
  echo
  # opened by double-click: keep the window up so the message can be read
  [[ -t 0 ]] && read -r -p "Press Enter to close…" _ || true
  exit 1
}

echo
echo "      J . A . R . V . I . S ."
echo "      first-time setup - this takes a few minutes"
echo

# ---- Python 3.10 - 3.14
PY=""
for c in python3.13 python3.12 python3.11 python3.14 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; exit(0 if (3,10)<=sys.version_info[:2]<=(3,14) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [[ -z "$PY" ]]; then
  if [[ "$OS" == "Darwin" ]]; then
    fail "Python 3.10-3.14 is required." "Install it with:  brew install python@3.12   (or from python.org), then run this again."
  elif command -v apt-get >/dev/null; then
    fail "Python 3.10-3.14 is required." "Install it with:  sudo apt install python3 python3-venv python3-pip"
  elif command -v dnf >/dev/null; then
    fail "Python 3.10-3.14 is required." "Install it with:  sudo dnf install python3 python3-pip"
  elif command -v pacman >/dev/null; then
    fail "Python 3.10-3.14 is required." "Install it with:  sudo pacman -S python python-pip"
  else
    fail "Python 3.10-3.14 is required." "Install it with your package manager, then run this again."
  fi
fi
echo "  • Python: $("$PY" --version)"

# ---- system libraries (voice needs PortAudio; Debian/Ubuntu split venv out of Python)
if [[ "$OS" == "Linux" ]]; then
  missing=()
  ldconfig -p 2>/dev/null | grep -q libportaudio || missing+=("libportaudio2")
  command -v xdg-open >/dev/null || missing+=("xdg-utils")
  if (( ${#missing[@]} )); then
    echo "  • Optional: for voice and opening files, install: ${missing[*]}"
    command -v apt-get >/dev/null && echo "      sudo apt install ${missing[*]}"
  fi
fi

# ---- the environment
if [[ -d "$VENV" && ! -f "$MARKER" ]] || [[ "$REINSTALL" == 1 ]]; then rm -rf "$VENV"; fi
if [[ ! -x "$VENV/bin/python" ]]; then
  # On Linux, see the system's python3-gi too, so the desktop window works where GTK is installed
  sysp=()
  [[ "$OS" == "Linux" ]] && sysp=(--system-site-packages)
  if ! "$PY" -m venv ${sysp[@]+"${sysp[@]}"} "$VENV" 2>/tmp/jarvis-venv.err; then
    rm -rf "$VENV"
    if grep -qi "ensurepip\|venv" /tmp/jarvis-venv.err && command -v apt-get >/dev/null; then
      ver="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
      fail "Python's venv module is missing." "Install it with:  sudo apt install python${ver}-venv   then run this again."
    fi
    fail "Couldn't create the Python environment:" "$(tail -3 /tmp/jarvis-venv.err)"
  fi
fi
"$VENV/bin/python" -m pip install --upgrade pip --quiet --disable-pip-version-check

EXTRAS="all"
command -v nvidia-smi >/dev/null 2>&1 && EXTRAS="all,gpu"
echo "  • Installing Jarvis ($EXTRAS)…"
if ! "$VENV/bin/python" -m pip install -e "$ROOT[$EXTRAS]" --disable-pip-version-check; then
  echo
  echo "  ! Some optional parts didn't install on this system. Trying the essentials…"
  "$VENV/bin/python" -m pip install -e "$ROOT[desktop,docs,voice]" --disable-pip-version-check \
    || "$VENV/bin/python" -m pip install -e "$ROOT" --disable-pip-version-check \
    || fail "Installation failed." "Scroll up for pip's error. Fix it, then run:  ./install.sh --reinstall"
  echo "  ! Installed without some extras - Jarvis works, a few tools will say what they're missing."
fi

# ---- app menu entry (Linux)
if [[ "$OS" == "Linux" ]]; then
  mkdir -p "$HOME/.local/share/applications"
  cat > "$HOME/.local/share/applications/jarvis.desktop" <<EOF
[Desktop Entry]
Name=Jarvis
Comment=J.A.R.V.I.S. desktop assistant
Exec="$ROOT/install.sh"
Icon=$ROOT/installer/jarvis-icon.png
Terminal=false
Type=Application
Categories=Utility;
EOF
  chmod +x "$HOME/.local/share/applications/jarvis.desktop" 2>/dev/null || true
fi

date > "$MARKER"
echo
echo "  ✓ Installed. Starting Jarvis…"
echo "    Next time: double-click Jarvis (app menu), Jarvis.desktop / Jarvis.command here, or run ./install.sh"
echo
exec "$VENV/bin/python" -m jarvis "$@"
