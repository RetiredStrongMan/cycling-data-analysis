"""Shared Strava client: token refresh, rate-limit-aware GET, .env persistence."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"

API_BASE = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"


def load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        sys.exit(f"Missing {ENV_PATH}. Copy .env.example to .env and fill it in.")
    load_dotenv(ENV_PATH, override=True)
    required = ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET"]
    env = {k: os.environ.get(k, "") for k in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN")}
    missing = [k for k in required if not env[k]]
    if missing:
        sys.exit(f"Missing required env vars: {', '.join(missing)}")
    return env


def write_env_value(key: str, value: str) -> None:
    """Update or append KEY=value in .env, preserving other lines."""
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

    def __init__(self) -> None:
        env = load_env()
        self.client_id = env["STRAVA_CLIENT_ID"]
        self.client_secret = env["STRAVA_CLIENT_SECRET"]
        self.refresh_token = env["STRAVA_REFRESH_TOKEN"]
        if not self.refresh_token:
            sys.exit(
                "STRAVA_REFRESH_TOKEN is empty. Run `python exchange_code.py <CODE>` first."
            )
        self.access_token: str | None = None
        self.access_token_expires_at: int = 0
        # Rate-limit state, updated from response headers.
        self.usage_15min = 0
        self.limit_15min = 100
        self.usage_daily = 0
        self.limit_daily = 1000
        self._ensure_access_token()

    # ---- Tokens ----
    def _ensure_access_token(self) -> None:
        now = int(time.time())
        if self.access_token and now < self.access_token_expires_at - 60:
            return
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        tok = resp.json()
        self.access_token = tok["access_token"]
        self.access_token_expires_at = int(tok["expires_at"])
        # Refresh token can rotate; persist if it changed.
        new_refresh = tok.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = new_refresh
            write_env_value("STRAVA_REFRESH_TOKEN", new_refresh)
            print("[strava] refresh_token rotated and saved to .env")

    # ---- Rate limit handling ----
    def _update_rate_limits(self, resp: requests.Response) -> None:
        usage = resp.headers.get("X-RateLimit-Usage", "")
        limit = resp.headers.get("X-RateLimit-Limit", "")
        # Read-specific headers (preferred when present).
        ru = resp.headers.get("X-ReadRateLimit-Usage")
        rl = resp.headers.get("X-ReadRateLimit-Limit")
        chosen_usage = ru or usage
        chosen_limit = rl or limit
        try:
            u15, ud = (int(x) for x in chosen_usage.split(","))
            l15, ld = (int(x) for x in chosen_limit.split(","))
            self.usage_15min, self.usage_daily = u15, ud
            self.limit_15min, self.limit_daily = l15, ld
        except Exception:
            pass

    def _throttle_if_needed(self) -> None:
        # If we've hit ~90% of the 15-min window, sleep to next quarter hour boundary.
        if self.limit_15min and self.usage_15min >= int(self.limit_15min * 0.9):
            now = time.time()
            # Strava 15-min windows align to wall-clock quarter-hours (UTC).
            seconds_into_window = int(now) % 900
            sleep_for = 900 - seconds_into_window + 5
            print(
                f"[strava] 15-min usage {self.usage_15min}/{self.limit_15min} — "
                f"sleeping {sleep_for}s for next window"
            )
            time.sleep(sleep_for)
        if self.limit_daily and self.usage_daily >= int(self.limit_daily * 0.97):
            print(
                f"[strava] daily usage {self.usage_daily}/{self.limit_daily} — "
                "stopping. Resume tomorrow (resets at UTC midnight)."
            )
            sys.exit(0)

    # ---- HTTP ----
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._ensure_access_token()
        self._throttle_if_needed()
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        for attempt in range(5):
            resp = requests.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=60,
            )
            self._update_rate_limits(resp)
            if resp.status_code == 429:
                # Hard rate-limit hit. Sleep to next window and retry.
                seconds_into_window = int(time.time()) % 900
                sleep_for = 900 - seconds_into_window + 5
                print(f"[strava] 429 received — sleeping {sleep_for}s")
                time.sleep(sleep_for)
                continue
            if resp.status_code == 401:
                # Token might have expired mid-flight; force refresh and retry once.
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
