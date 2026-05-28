"""Background worker for backfill + webhook-driven sync jobs.

Thread-pool based. Adequate for a single-process Flask deployment with low
concurrency (≤ ~50 active users). For higher concurrency or job durability
across process restarts, swap this module for an RQ/Celery setup — the
public surface (`submit_backfill`, `submit_sync_one`) stays the same.

State that survives restarts lives in `users.backfill_state` /
`backfill_progress` / `backfill_total` so the UI can poll progress even
if the worker thread dies.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import storage

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coach-worker")
_inflight: dict[int, str] = {}     # user_id -> job kind
_inflight_lock = threading.Lock()


def _claim(user_id: int, kind: str) -> bool:
    """Reserve a slot for this user+kind. Returns False if already running."""
    with _inflight_lock:
        if user_id in _inflight:
            return False
        _inflight[user_id] = kind
        return True


def _release(user_id: int) -> None:
    with _inflight_lock:
        _inflight.pop(user_id, None)


# ---------------------------------------------------------------------
#                          BACKFILL
# ---------------------------------------------------------------------

def submit_backfill(user_id: int) -> bool:
    """Start a background full-history backfill for `user_id`.

    Returns False if a backfill is already running for this user.
    Persists state to the users table so the UI can poll.
    """
    if not _claim(user_id, "backfill"):
        log.info("backfill u=%s already inflight, skipping", user_id)
        return False
    _executor.submit(_run_backfill, user_id)
    return True


def _run_backfill(user_id: int) -> None:
    """Worker body: mark running, do the work, mark done/failed."""
    try:
        conn = storage.connect()
        try:
            storage.update_backfill_state(conn, user_id, state="running")
        finally:
            conn.close()

        # Lazy import to avoid circular module loads at startup
        from backfill import run_for_user
        run_for_user(user_id, summaries=True, streams=True)

        conn = storage.connect()
        try:
            storage.update_backfill_state(conn, user_id, state="done")
        finally:
            conn.close()
        log.info("backfill u=%s complete", user_id)
    except Exception:
        log.exception("backfill u=%s failed", user_id)
        try:
            conn = storage.connect()
            try:
                storage.update_backfill_state(conn, user_id, state="failed")
            finally:
                conn.close()
        except Exception:
            log.exception("failed to mark u=%s as failed", user_id)
    finally:
        _release(user_id)


# ---------------------------------------------------------------------
#                       WEBHOOK-DRIVEN SYNC
# ---------------------------------------------------------------------

def submit_sync_one(user_id: int, activity_id: int, aspect_type: str = "create") -> bool:
    """Pull a single activity (typically from a Strava webhook event).

    Multiple events for the same user are processed serially via the worker
    pool; the per-user lock prevents two webhook jobs for the same user from
    fighting each other.
    """
    job_key = f"sync_one:{activity_id}"
    if not _claim(user_id, job_key):
        log.info("sync_one u=%s a=%s collides with %s, skipping",
                 user_id, activity_id, _inflight.get(user_id))
        return False
    _executor.submit(_run_sync_one, user_id, activity_id, aspect_type)
    return True


def _run_sync_one(user_id: int, activity_id: int, aspect_type: str) -> None:
    try:
        from strava import StravaClient
        from backfill import STREAM_KEYS, now_iso, CYCLING_SPORT_TYPES

        client = StravaClient.for_user(user_id)
        try:
            if aspect_type == "delete":
                client.conn.execute(
                    "DELETE FROM activities WHERE user_id = ? AND id = ?",
                    (user_id, activity_id),
                )
                client.conn.commit()
                log.info("sync_one u=%s a=%s: deleted", user_id, activity_id)
                return
            # Create or update — fetch summary + streams (if cycling)
            summary = client.get(f"/activities/{activity_id}")
            storage.upsert_summary(client.conn, user_id, summary)
            client.conn.commit()
            if summary.get("sport_type") in CYCLING_SPORT_TYPES:
                try:
                    data = client.get(
                        f"/activities/{activity_id}/streams",
                        {"keys": STREAM_KEYS, "key_by_type": "true"},
                    )
                    storage.save_streams(user_id, activity_id, data)
                    storage.mark_streams_fetched(client.conn, user_id, activity_id, now_iso())
                    client.conn.commit()
                except Exception:
                    log.exception("streams fetch failed for u=%s a=%s",
                                  user_id, activity_id)
            log.info("sync_one u=%s a=%s: %s", user_id, activity_id, aspect_type)
        finally:
            client.conn.close()
    except Exception:
        log.exception("sync_one u=%s a=%s failed", user_id, activity_id)
    finally:
        _release(user_id)


# ---------------------------------------------------------------------
#                        STARTUP HOUSEKEEPING
# ---------------------------------------------------------------------

def reconcile_on_startup() -> None:
    """Look for users left in 'running' state from a previous process and
    mark them 'failed'. The dashboard will offer them a Retry button.

    Without this, a user whose backfill was interrupted by a server restart
    would see 'running' forever and never get a chance to retry.
    """
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM users WHERE backfill_state = 'running'"
        ).fetchall()
        for r in rows:
            log.warning("reconcile: marking u=%s backfill as failed (process restart)", r[0])
            storage.update_backfill_state(conn, r[0], state="failed")
    finally:
        conn.close()


def shutdown(wait: bool = False) -> None:
    """Stop accepting jobs. Call on graceful shutdown."""
    _executor.shutdown(wait=wait, cancel_futures=not wait)
