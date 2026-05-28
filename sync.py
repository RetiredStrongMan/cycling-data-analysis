"""Incremental sync — for one user.

Usage:
    python sync.py                       # default: user_id=1
    python sync.py --user-id 1 --days 30
"""
from __future__ import annotations

import argparse
import datetime as dt
import time

import storage
from backfill import CYCLING_SPORT_TYPES, backfill_streams, now_iso
from strava import StravaClient


def fetch_after(client: StravaClient, after_epoch: int) -> int:
    user_id = client.user.id
    conn = client.conn
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
        existing = storage.known_ids(conn, user_id)
        for act in batch:
            if act["id"] not in existing:
                new += 1
            storage.upsert_summary(conn, user_id, act)
        conn.commit()
        if len(batch) < per_page:
            break
        page += 1
    return new


def run_for_user(user_id: int, *, days: int | None = None, with_streams: bool = True):
    client = StravaClient.for_user(user_id)
    try:
        last = storage.get_state(client.conn, user_id, "last_sync_epoch")
        now_epoch = int(time.time())
        if days is not None:
            after = now_epoch - days * 86400
        elif last:
            after = max(0, int(last) - 3600)  # 1h overlap for late edits
        else:
            after = now_epoch - 14 * 86400
        print(f"[sync u={user_id}] fetching after epoch={after} "
              f"({dt.datetime.fromtimestamp(after, dt.timezone.utc).isoformat()})")
        new_count = fetch_after(client, after)
        print(f"[sync u={user_id}] {new_count} new/updated summaries")

        if with_streams:
            pulled = backfill_streams(client, CYCLING_SPORT_TYPES)
            print(f"[sync u={user_id}] streams pulled: {pulled}")

        storage.set_state(client.conn, user_id, "last_sync_epoch", str(now_epoch))
        storage.set_state(client.conn, user_id, "last_sync_iso", now_iso())
        # Bump last_sync_at on the user row too (used by UI / scheduler)
        client.conn.execute("UPDATE users SET last_sync_at = ? WHERE id = ?",
                            (now_iso(), user_id))
        client.conn.commit()
        print(f"[sync u={user_id}] last_sync_epoch = {now_epoch}")
    finally:
        client.conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--user-id", type=int, default=1)
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--no-streams", action="store_true")
    args = p.parse_args()
    run_for_user(args.user_id, days=args.days, with_streams=not args.no_streams)


if __name__ == "__main__":
    main()
