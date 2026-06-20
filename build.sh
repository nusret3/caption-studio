#!/usr/bin/env bash
# Build a standalone Caption Studio folder with PyInstaller (onedir).
# Output: dist/CaptionStudio/CaptionStudio
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x venv/bin/python ]; then
  echo "venv not found. Run ./setup.sh first."
  exit 1
fi

./venv/bin/python -m pip install --upgrade pyinstaller
./venv/bin/python -m PyInstaller --noconfirm caption_studio.spec

echo
echo "Built dist/CaptionStudio/  (zip and share that folder)"
