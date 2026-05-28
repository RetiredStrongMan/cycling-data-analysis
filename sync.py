"""Incremental sync: pull activities added/edited since the last successful run.

Strategy:
  - Track `last_sync_epoch` in sync_state table.
  - Query /athlete/activities?after=<epoch>&per_page=50 (cheap; 1-N pages).
  - Upsert summaries; for any new cycling activity, also pull streams.

Run on demand (manual) or via cron / launchd. Designed to use a few API calls
per run so it's safe at hourly cadence.

Usage:
    python sync.py             # incremental since last run (or last 14d on first run)
    python sync.py --days 30   # explicit window
"""
from __future__ import annotations

import argparse
import datetime as dt
import time

import storage
from backfill import (
    CYCLING_SPORT_TYPES,
    STREAM_KEYS,
    backfill_streams,
    now_iso,
)
from strava import StravaClient


def fetch_after(client: StravaClient, after_epoch: int) -> int:
    conn = storage.connect()
    page = 1
    per_page = 50
    new = 0
    while True:
        batch = client.get(
            "/athlete/activities",
            {"per_page": per_page, "page": page, "after": after_epoch},
        )
        if not batch:
            break
        existing = storage.known_ids(conn)
        for act in batch:
            if act["id"] not in existing:
                new += 1
            storage.upsert_summary(conn, act)
        conn.commit()
        if len(batch) < per_page:
            break
        page += 1
    conn.close()
    return new


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=None, help="Sync window in days (overrides last_sync)")
    p.add_argument("--no-streams", action="store_true", help="Skip stream fetching")
    args = p.parse_args()

    client = StravaClient()
    conn = storage.connect()
    last = storage.get_state(conn, "last_sync_epoch")
    conn.close()

    now_epoch = int(time.time())
    if args.days is not None:
        after = now_epoch - args.days * 86400
    elif last:
        # Re-fetch with a small overlap to catch late edits.
        after = max(0, int(last) - 3600)
    else:
        after = now_epoch - 14 * 86400
        print(f"[sync] first run; using 14-day window")

    after_iso = dt.datetime.fromtimestamp(after, dt.timezone.utc).isoformat()
    print(f"[sync] fetching activities after {after_iso} (epoch={after})")

    new_count = fetch_after(client, after)
    print(f"[sync] {new_count} new/updated activity summaries")

    if not args.no_streams:
        pulled = backfill_streams(client, CYCLING_SPORT_TYPES)
        print(f"[sync] streams pulled: {pulled}")

    conn = storage.connect()
    storage.set_state(conn, "last_sync_epoch", str(now_epoch))
    storage.set_state(conn, "last_sync_iso", now_iso())
    conn.commit()
    conn.close()
    print(f"[sync] last_sync_epoch updated to {now_epoch}")


if __name__ == "__main__":
    main()
