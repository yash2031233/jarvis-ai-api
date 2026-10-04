#!/usr/bin/env bash
# Jarvis one-step install + launch for macOS and Linux.
#   ./install.sh            install (first time) and start
#   ./install.sh --reinstall
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
MARKER="$VENV/.jarvis-installed"

if [[ "${1:-}" != "--reinstall" && -f "$MARKER" ]]; then
  exec "$VENV/bin/python" -m jarvis "$@"
fi

echo
echo "      J . A . R . V . I . S ."
echo "      first-time setup - this takes a few minutes"
echo

PY=""
for c in python3.12 python3.13 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; exit(0 if (3,10)<=sys.version_info[:2]<=(3,13) else 1)'; then
    PY="$c"; break
  fi
done
if [[ -z "$PY" ]]; then
  echo "Python 3.10-3.13 is required."
  if [[ "$(uname)" == "Darwin" ]]; then echo "Install it with:  brew install python@3.12"; else echo "Install it with your package manager, e.g.  sudo apt install python3.12 python3.12-venv"; fi
  exit 1
fi

if [[ "$(uname)" == "Linux" ]]; then
  missing=()
  ldconfig -p 2>/dev/null | grep -q libportaudio || missing+=("libportaudio2")
  command -v xdg-open >/dev/null || missing+=("xdg-utils")
  if (( ${#missing[@]} )); then echo "Tip: for voice/desktop features install: sudo apt install ${missing[*]}"; fi
fi

[[ "${1:-}" == "--reinstall" ]] && rm -rf "$VENV"
[[ -x "$VENV/bin/python" ]] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip --quiet --disable-pip-version-check
EXTRAS="all"
command -v nvidia-smi >/dev/null 2>&1 && EXTRAS="all,gpu"
"$VENV/bin/python" -m pip install -e "$ROOT[$EXTRAS]" --disable-pip-version-check

if [[ "$(uname)" == "Linux" && -d "$HOME/.local/share/applications" ]]; then
  cat > "$HOME/.local/share/applications/jarvis.desktop" <<EOF
[Desktop Entry]
Name=Jarvis
Comment=J.A.R.V.I.S. desktop assistant
Exec=$ROOT/install.sh
Icon=$ROOT/installer/jarvis-icon.png
Terminal=false
Type=Application
Categories=Utility;
EOF
fi

date > "$MARKER"
echo "Starting Jarvis… (next time just run ./install.sh again, or use the app menu entry)"
exec "$VENV/bin/python" -m jarvis
