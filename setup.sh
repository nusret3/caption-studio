#!/usr/bin/env bash
# Create a virtual environment and install Caption Studio's dependencies.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install -r requirements.txt

echo
echo "Setup complete. Run the app with: ./run.sh"
