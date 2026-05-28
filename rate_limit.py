"""Process-global Strava API rate limiter.

Strava's read limit is **per app**, not per user. With multiple users
backfilling concurrently the 100-reads / 15-min and 1000-reads / 24-h caps
become a shared resource. Every API request acquires a token from this
bucket first, blocking when the bucket is dry until the next window.

Single-process, thread-safe. When you move to multi-process workers
(gunicorn with >1 worker, or a separate worker container) replace this
with a Redis-backed bucket — same interface.

Strava's response headers (X-ReadRateLimit-Usage / X-ReadRateLimit-Limit)
are authoritative; `update_from_response()` syncs our counters after each
request so we don't drift from Strava's view.
"""
from __future__ import annotations

import threading
import time

# Default Strava sandbox limits. update_from_response() rewrites them based
# on whatever Strava actually says (production-tier apps get higher numbers).
_DEFAULT_15MIN = 100
_DEFAULT_DAILY = 1000

# 15-min windows align to wall-clock quarter hours (UTC).
_WINDOW_15 = 900
_WINDOW_DAY = 86400


class TokenBucket:
    def __init__(self, cap_15min: int = _DEFAULT_15MIN, cap_daily: int = _DEFAULT_DAILY):
        self.cap_15min = cap_15min
        self.cap_daily = cap_daily
        self.usage_15min = 0
        self.usage_daily = 0
        self.window_15_start = self._window_15_start_now()
        self.window_day_start = self._window_day_start_now()
        self._cond = threading.Condition()

    # ---- window helpers ----
    @staticmethod
    def _window_15_start_now() -> int:
        now = int(time.time())
        return now - (now % _WINDOW_15)

    @staticmethod
    def _window_day_start_now() -> int:
        now = int(time.time())
        return now - (now % _WINDOW_DAY)

    def _roll_windows(self) -> None:
        """Reset counters if we've crossed a window boundary."""
        now_15 = self._window_15_start_now()
        if now_15 > self.window_15_start:
            self.window_15_start = now_15
            self.usage_15min = 0
        now_day = self._window_day_start_now()
        if now_day > self.window_day_start:
            self.window_day_start = now_day
            self.usage_daily = 0

    # ---- public API ----
    def acquire(self, n: int = 1) -> None:
        """Block until n tokens are available, then consume them."""
        with self._cond:
            while True:
                self._roll_windows()
                if self.usage_15min + n <= self.cap_15min and \
                   self.usage_daily + n <= self.cap_daily:
                    self.usage_15min += n
                    self.usage_daily += n
                    return
                # Compute sleep until the next refill opportunity
                if self.usage_15min + n > self.cap_15min:
                    target = self.window_15_start + _WINDOW_15
                else:
                    target = self.window_day_start + _WINDOW_DAY
                sleep_for = max(2, target - int(time.time()) + 1)
                # Wait on the condition so update_from_response() can poke us awake
                self._cond.wait(timeout=min(sleep_for, 60))

    def update_from_response(self, usage_15min: int, limit_15min: int,
                              usage_daily: int, limit_daily: int) -> None:
        """Sync counters with Strava's response headers (authoritative)."""
        with self._cond:
            # Strava's view trumps ours — they count requests we may have
            # forgotten about (e.g. across restarts).
            self.usage_15min = max(self.usage_15min, usage_15min)
            self.usage_daily = max(self.usage_daily, usage_daily)
            self.cap_15min = limit_15min or self.cap_15min
            self.cap_daily = limit_daily or self.cap_daily
            self._cond.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self._cond:
            self._roll_windows()
            return {
                "usage_15min": self.usage_15min, "limit_15min": self.cap_15min,
                "usage_daily": self.usage_daily, "limit_daily": self.cap_daily,
                "window_15_resets_in": self.window_15_start + _WINDOW_15 - int(time.time()),
                "window_day_resets_in": self.window_day_start + _WINDOW_DAY - int(time.time()),
            }


# Module-level singleton — every StravaClient shares this.
GLOBAL = TokenBucket()
