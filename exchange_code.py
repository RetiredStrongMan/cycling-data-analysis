"""One-time: exchange the OAuth `code` from your redirect URL for a refresh_token.

Usage:
    python exchange_code.py <CODE>

Reads STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET from .env, posts to
https://www.strava.com/oauth/token, then writes STRAVA_REFRESH_TOKEN back to .env.
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python exchange_code.py <CODE>")
    code = sys.argv[1].strip()

    if not ENV_PATH.exists():
        sys.exit(f"Missing {ENV_PATH}. Copy .env.example to .env and fill in CLIENT_ID/SECRET first.")
    load_dotenv(ENV_PATH, override=True)

    import os

    client_id = os.environ.get("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        sys.exit("STRAVA_CLIENT_ID or STRAVA_CLIENT_SECRET missing in .env")

    print(f"[exchange] POST /oauth/token (client_id={client_id})")
    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        sys.exit(f"Exchange failed ({resp.status_code}): {resp.text}")

    body = resp.json()
    refresh_token = body.get("refresh_token")
    scope = body.get("scope") or ",".join(body.get("athlete", {}).get("scopes", []))
    athlete = body.get("athlete", {})
    if not refresh_token:
        sys.exit(f"No refresh_token in response: {body}")

    # Write/replace STRAVA_REFRESH_TOKEN in .env (preserving other lines).
    from strava import write_env_value

    write_env_value("STRAVA_REFRESH_TOKEN", refresh_token)

    print("[exchange] success.")
    print(f"  athlete : {athlete.get('firstname', '')} {athlete.get('lastname', '')} (id={athlete.get('id')})")
    print(f"  scope   : {scope}")
    print(f"  refresh_token saved to .env (not printed for safety)")
    print("\nNext: python backfill.py")


if __name__ == "__main__":
    main()
