#!/usr/bin/env bash
# Run a first scrape (if DB is empty) then start the API server.
# Usage: ./dev.sh
set -e

cd "$(dirname "$0")"

# Create virtual environment on first run
if [ ! -d ".venv" ]; then
    echo "=== Creating virtual environment ==="
    python3 -m venv .venv
fi

# Activate venv
source .venv/bin/activate

echo "=== Installing / checking dependencies ==="
pip install -r requirements.txt -q

echo "=== Running updater (first scrape may take a few minutes) ==="
python3 updater.py

echo ""
echo "=== Starting API server at http://localhost:8000 ==="
echo "    Open http://localhost:8000 in your browser"
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
