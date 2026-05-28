#!/usr/bin/env bash
# Launch Wilson's AI Coach locally and open it in your browser.
# Uses http://localhost:5001/ so Strava's OAuth callback (which expects the
# "localhost" callback domain) works on first sign-in.
set -e
cd "$(dirname "$0")"
source .venv/bin/activate

(sleep 2 && open "http://localhost:5001") &
python app.py
