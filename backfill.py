"""Backfill all historical Strava activities + streams — for one user.

Usage (CLI):
    python backfill.py                          # default: user_id=1
    python backfill.py --user-id 1 --summaries
    python backfill.py --user-id 1 --streams
    python backfill.py --user-id 1 --all-sports
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

import storage
from strava import StravaClient

CYCLING_SPORT_TYPES = {
    "Ride", "VirtualRide", "MountainBikeRide", "GravelRide", "EBikeRide",
    "EMountainBikeRide", "Velomobile", "Handcycle",
}

STREAM_KEYS = (
    "time,distance,latlng,altitude,velocity_smooth,heartrate,cadence,watts,temp,"
    "moving,grade_smooth"
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def backfill_summaries(client: StravaClient) -> int:
    user_id = client.user.id
    conn = client.conn
    total_new = 0
    page = 1
    per_page = 200
    while True:
        print(f"[summaries u={user_id}] page {page}  "
              f"15min={client.usage_15min}/{client.limit_15min}  "
              f"daily={client.usage_daily}/{client.limit_daily}")
        batch = client.get("/athlete/activities", {"per_page": per_page, "page": page})
        if not batch:
            break
        existing = storage.known_ids(conn, user_id)
        new = 0
        for act in batch:
            if act["id"] not in existing:
                new += 1
            storage.upsert_summary(conn, user_id, act)
        conn.commit()
        total_new += new
        print(f"  +{new} new (of {len(batch)} fetched, {len(existing)} previously known)")
        if len(batch) < per_page:
            break
        page += 1
    print(f"[summaries u={user_id}] done. {total_new} new activity summaries.")
    return total_new


def backfill_streams(client: StravaClient, sport_filter: set[str] | None) -> int:
    user_id = client.user.id
    conn = client.conn
    ids = storage.ids_missing_streams(conn, user_id, sport_types=sport_filter)
    print(f"[streams u={user_id}] {len(ids)} activities missing streams")
    if not ids:
        return 0
    pulled = 0
    storage.update_backfill_state(conn, user_id, state="running", total=len(ids))
    for i, aid in enumerate(ids, 1):
        try:
            data = client.get(
                f"/activities/{aid}/streams",
                {"keys": STREAM_KEYS, "key_by_type": "true"},
            )
        except Exception as e:
            print(f"[streams u={user_id}] {aid}: failed ({e}); skipping")
            continue
        storage.save_streams(user_id, aid, data)
        storage.mark_streams_fetched(conn, user_id, aid, now_iso())
        conn.commit()
        pulled += 1
        storage.update_backfill_state(conn, user_id, progress=pulled)
        if i % 10 == 0 or i == len(ids):
            print(f"[streams u={user_id}] {i}/{len(ids)}  pulled={pulled}  "
                  f"15min={client.usage_15min}/{client.limit_15min}  "
                  f"daily={client.usage_daily}/{client.limit_daily}")
    storage.update_backfill_state(conn, user_id, state="done")
    return pulled


def backfill_details(client: StravaClient, sport_filter: set[str] | None) -> int:
    user_id = client.user.id
    conn = client.conn
    if sport_filter:
        marks = ",".join("?" * len(sport_filter))
        rows = conn.execute(
            f"SELECT id FROM activities WHERE user_id = ? AND detail_fetched_at IS NULL "
            f"AND sport_type IN ({marks}) ORDER BY start_date DESC",
            (user_id, *sport_filter),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM activities WHERE user_id = ? AND detail_fetched_at IS NULL "
            "ORDER BY start_date DESC",
            (user_id,),
        ).fetchall()
    ids = [r[0] for r in rows]
    print(f"[details u={user_id}] {len(ids)} activities missing detail")
    pulled = 0
    for i, aid in enumerate(ids, 1):
        try:
            detail = client.get(f"/activities/{aid}", {"include_all_efforts": "false"})
        except Exception as e:
            print(f"[details u={user_id}] {aid}: failed ({e}); skipping")
            continue
        storage.upsert_detail(conn, user_id, detail, now_iso())
        conn.commit()
        pulled += 1
        if i % 10 == 0 or i == len(ids):
            print(f"[details u={user_id}] {i}/{len(ids)}  pulled={pulled}  "
                  f"15min={client.usage_15min}/{client.limit_15min}")
    return pulled


def run_for_user(user_id: int, *, summaries=True, streams=True, details=False,
                  all_sports=False) -> None:
    client = StravaClient.for_user(user_id)
    sport_filter = None if all_sports else CYCLING_SPORT_TYPES
    try:
        if summaries:
            backfill_summaries(client)
        if streams:
            backfill_streams(client, sport_filter)
        if details:
            backfill_details(client, sport_filter)
    finally:
        client.conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--user-id", type=int, default=1)
    p.add_argument("--summaries", action="store_true")
    p.add_argument("--streams", action="store_true")
    p.add_argument("--details", action="store_true")
    p.add_argument("--all-sports", action="store_true")
    args = p.parse_args()

    only_some = args.summaries or args.streams or args.details
    run_for_user(
        args.user_id,
        summaries=args.summaries or not only_some,
        streams=args.streams or not only_some,
        details=args.details,
        all_sports=args.all_sports,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[backfill] interrupted; safe to re-run.")
        sys.exit(130)
