"""Strava HTTP client — user-aware edition.

Tokens are loaded from the `users` table; refreshes write back to the DB.
The Strava client_id and client_secret are app-level (env vars).

Two ways to construct:
    StravaClient.for_user(user_id)        # opens its own DB connection
    StravaClient(user, db_conn)           # caller supplies an open connection
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

import rate_limit
import storage

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"

API_BASE = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"


# Load .env at import time so app server + CLI scripts both pick up client creds.
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


def _app_credentials() -> tuple[str, str]:
    client_id = os.environ.get("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        sys.exit("Missing STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET in .env")
    return client_id, client_secret


def write_env_value(key: str, value: str) -> None:
    """Update or append KEY=value in .env. Kept for the legacy CLI scripts."""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")


class StravaClient:
    """Handles token refresh, rate-limit headers, and throttled GETs."""

    def __init__(self, user: storage.User, conn: storage.sqlite3.Connection):
        self.user = user
        self.conn = conn
        self.client_id, self.client_secret = _app_credentials()
        self.access_token: str | None = user.access_token
        self.access_token_expires: int = user.access_token_expires or 0
        self.refresh_token: str = user.refresh_token
        # Rate-limit headers, refreshed after each call
        self.usage_15min = 0
        self.limit_15min = 100
        self.usage_daily = 0
        self.limit_daily = 1000
        self._ensure_access_token()

    @classmethod
    def for_user(cls, user_id: int) -> "StravaClient":
        conn = storage.connect()
        user = storage.get_user(conn, user_id)
        if user is None:
            conn.close()
            sys.exit(f"No such user: {user_id}")
        return cls(user, conn)

    # ---- Tokens ----
    def _ensure_access_token(self) -> None:
        now = int(time.time())
        if self.access_token and now < self.access_token_expires - 60:
            return
        resp = requests.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }, timeout=30)
        resp.raise_for_status()
        tok = resp.json()
        self.access_token = tok["access_token"]
        self.access_token_expires = int(tok["expires_at"])
        new_refresh = tok.get("refresh_token")
        if new_refresh:
            self.refresh_token = new_refresh
        # Persist immediately so the next request from any worker picks up the
        # rotated token.
        storage.update_user_tokens(
            self.conn, self.user.id,
            refresh_token=self.refresh_token,
            access_token=self.access_token,
            access_token_expires=self.access_token_expires,
        )

    # ---- Rate-limit handling ----
    def _update_rate_limits(self, resp: requests.Response) -> None:
        ru = resp.headers.get("X-ReadRateLimit-Usage")
        rl = resp.headers.get("X-ReadRateLimit-Limit")
        usage = ru or resp.headers.get("X-RateLimit-Usage", "")
        limit = rl or resp.headers.get("X-RateLimit-Limit", "")
        try:
            u15, ud = (int(x) for x in usage.split(","))
            l15, ld = (int(x) for x in limit.split(","))
            self.usage_15min, self.usage_daily = u15, ud
            self.limit_15min, self.limit_daily = l15, ld
            # Push Strava's authoritative numbers into the process-global bucket
            # so all workers see the same view.
            rate_limit.GLOBAL.update_from_response(u15, l15, ud, ld)
        except Exception:
            pass

    # ---- HTTP ----
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._ensure_access_token()
        # Process-global rate-limit gate — blocks until both the 15-min and
        # daily Strava quotas have budget. Coordinates across worker threads.
        rate_limit.GLOBAL.acquire(1)
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        for attempt in range(5):
            resp = requests.get(
                url, params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=60,
            )
            self._update_rate_limits(resp)
            if resp.status_code == 429:
                seconds_into_window = int(time.time()) % 900
                sleep_for = 900 - seconds_into_window + 5
                print(f"[strava] 429 received — sleeping {sleep_for}s")
                time.sleep(sleep_for)
                continue
            if resp.status_code == 401:
                self.access_token = None
                self._ensure_access_token()
                continue
            if 500 <= resp.status_code < 600:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()  # type: ignore[name-defined]
        return None


class StravaQuotaExhausted(Exception):
    pass
