"""Shared per-user computation caches.

Lives in its own module so BOTH the web process (app.py) and the background
worker (worker.py) can read and — crucially — invalidate the same caches
without a circular import (app → worker → app).

In the production deployment (gunicorn: 1 worker process, N threads) these
module-level lru_caches are shared across every request thread and worker
thread in the process. So when a backfill or webhook-driven sync finishes and
calls `invalidate()`, the very next web request recomputes from fresh data —
no stale dashboard, no disagreement between pages.

Everything user-facing that needs the Power-Duration model goes through here,
which is what guarantees mFTP / FRC / TTE / Pmax / Stamina / zones / TSS are
identical across every module.
"""
from __future__ import annotations

from functools import lru_cache

import pandas as pd

import wko


@lru_cache(maxsize=64)
def pdc(user_id: int) -> pd.DataFrame:
    """Cached Power-Duration Curve for a user."""
    return wko.power_duration_curve(user_id)


@lru_cache(maxsize=64)
def pd_model(user_id: int) -> wko.PDModel:
    """Cached modeled PD parameters (mFTP, FRC, TTE, Pmax, Stamina, CP, W')."""
    return wko.fit_pd_model(pdc(user_id))


def invalidate(user_id: int | None = None) -> None:
    """Drop cached PDC / model after a user's ride data changes.

    lru_cache has no per-key eviction, so we clear everything. Recomputing a
    single user's PDC is cheap relative to the cost of ever showing stale data.
    `user_id` is accepted for call-site clarity / future per-key caching.
    """
    pdc.cache_clear()
    pd_model.cache_clear()
