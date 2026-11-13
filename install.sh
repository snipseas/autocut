#!/usr/bin/env bash
# Set autocut up in this folder: Python environment + the `autocut` command. Safe to run again.
#   ./install.sh            install what is missing
#   ./install.sh --upgrade  also update the Python packages
set -euo pipefail
here=$(dirname "$(realpath "${BASH_SOURCE[0]}")")
upgrade=""; [ "${1:-}" = --upgrade ] && upgrade="--upgrade"
bin="${AUTOCUT_BIN:-$HOME/.local/bin}"

# 1. programs from your package manager
missing=()
for c in ffmpeg ffprobe fc-match column; do command -v "$c" >/dev/null || missing+=("$c"); done
if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing programs: ${missing[*]}"
  echo "  Arch:          sudo pacman -S ffmpeg fontconfig util-linux"
  echo "  Debian/Ubuntu: sudo apt install ffmpeg fontconfig bsdextrautils"
  echo "  Fedora:        sudo dnf install ffmpeg fontconfig util-linux"
  echo "Install them and run this again."
  exit 1
fi

# 2. Python environment (uv if you have it: it can fetch a Python that the speech packages support)
py="$here/venv/bin/python"
if [ ! -x "$py" ]; then
  echo "Creating the Python environment ..."
  if command -v uv >/dev/null; then uv venv --python 3.12 "$here/venv"
  else python3 -m venv "$here/venv"; fi
fi
if [ -n "$upgrade" ] || ! "$py" -I -c 'import faster_whisper, numpy' 2>/dev/null; then
  echo "Installing Python packages ..."
  if command -v uv >/dev/null; then uv pip install $upgrade --python "$py" -r "$here/requirements.txt"
  else "$py" -m pip install $upgrade -r "$here/requirements.txt"; fi
fi

# 3. the command
chmod +x "$here/autocut" "$here/render.sh"
mkdir -p "$bin"
if [ "$(realpath "$bin/autocut" 2>/dev/null || true)" != "$here/autocut" ]; then
  ln -sfn "$here/autocut" "$bin/autocut"
  echo "Linked $bin/autocut"
fi
case ":$PATH:" in *":$bin:"*) ;; *) echo "Note: $bin is not on your PATH; add it, or run $here/autocut directly." ;; esac

echo
"$here/autocut" --check || true
if [ ! -s "$here/brands" ]; then
  echo
  echo "Next: give autocut a finished project to copy your style from:"
  echo "  autocut --set-brand NAME \"/path/to/a finished video.kdenlive\""
fi
