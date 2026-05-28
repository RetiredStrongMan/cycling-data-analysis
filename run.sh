#!/usr/bin/env bash
# Launch Wilson's AI Coach locally. Opens http://127.0.0.1:5000 in your browser.
set -e
cd "$(dirname "$0")"
source .venv/bin/activate

# Sync first (cheap; respects rate limits) — comment out to skip.
if [ "$1" != "--no-sync" ]; then
  python sync.py || echo "[run] sync failed; continuing with existing data"
fi

# Open browser after a short delay
(sleep 2 && open http://127.0.0.1:5001) &

python app.py
