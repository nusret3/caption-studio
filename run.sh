#!/usr/bin/env bash
# Launch Caption Studio from the virtual environment.
# Optional: pass a video path, e.g. ./run.sh ~/clips/myclip.mp4
cd "$(dirname "$0")"
./venv/bin/python caption_studio.py "$@"
