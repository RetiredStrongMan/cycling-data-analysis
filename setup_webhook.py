"""One-time CLI to create / inspect / delete the Strava push subscription.

Run from your local machine (not Fly) — it just hits Strava's API with the
client_id/secret from your .env. The callback_url and verify_token must
match what the deployed server expects.

Usage:
    # Confirm what's currently registered (no auth wall — Strava lets the app
    # owner list its own subs)
    python setup_webhook.py list

    # Register the subscription. The callback_url must be reachable from the
    # public internet (i.e. your deployed Fly URL).
    python setup_webhook.py create https://your-app.fly.dev/strava/webhook

    # Delete (Strava only allows one subscription per app at a time)
    python setup_webhook.py delete 12345
"""
from __future__ import annotations

import os
import secrets
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
VERIFY_TOKEN = os.environ.get("STRAVA_VERIFY_TOKEN", "").strip()

API = "https://www.strava.com/api/v3/push_subscriptions"


def _need_creds():
    if not (CLIENT_ID and CLIENT_SECRET):
        sys.exit("STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET missing in .env")


def list_subs():
    _need_creds()
    r = requests.get(API, params={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}, timeout=15)
    r.raise_for_status()
    subs = r.json()
    if not subs:
        print("No subscriptions registered.")
        return
    for s in subs:
        print(f"  id={s['id']}  callback_url={s['callback_url']}  created_at={s['created_at']}")


def create_sub(callback_url: str):
    _need_creds()
    global VERIFY_TOKEN
    if not VERIFY_TOKEN:
        # Generate one and tell the user to set it in their .env / fly secrets
        VERIFY_TOKEN = secrets.token_hex(16)
        print(f"[setup] No STRAVA_VERIFY_TOKEN in .env. Generated: {VERIFY_TOKEN}")
        print(f"[setup] Add this to .env AND `fly secrets set STRAVA_VERIFY_TOKEN={VERIFY_TOKEN}` before continuing.")
        sys.exit(1)
    r = requests.post(API, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "callback_url": callback_url,
        "verify_token": VERIFY_TOKEN,
    }, timeout=30)
    if r.status_code != 201:
        sys.exit(f"create failed ({r.status_code}): {r.text}")
    print(f"Subscription created: {r.json()}")


def delete_sub(sub_id: int):
    _need_creds()
    r = requests.delete(
        f"{API}/{sub_id}",
        params={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=15,
    )
    if r.status_code != 204:
        sys.exit(f"delete failed ({r.status_code}): {r.text}")
    print(f"Deleted subscription {sub_id}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "list":
        list_subs()
    elif cmd == "create":
        if len(sys.argv) < 3:
            sys.exit("Usage: python setup_webhook.py create <callback_url>")
        create_sub(sys.argv[2])
    elif cmd == "delete":
        if len(sys.argv) < 3:
            sys.exit("Usage: python setup_webhook.py delete <subscription_id>")
        delete_sub(int(sys.argv[2]))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
