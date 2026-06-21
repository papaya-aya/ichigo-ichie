#!/usr/bin/env bash
# Start the Ichigo Ichie shift manager.
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "Creating virtual environment…"
  python3 -m venv venv
  ./venv/bin/pip install --quiet --upgrade pip
  ./venv/bin/pip install --quiet -r requirements.txt
fi

PORT="${PORT:-5001}"
echo "Starting on http://localhost:${PORT}  (Ctrl+C to stop)"
./venv/bin/python app.py
