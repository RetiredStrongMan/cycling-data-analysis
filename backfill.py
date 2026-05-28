"""Backfill all historical Strava activities + streams.

Phases:
  1. Page /athlete/activities (summary records) and upsert into SQLite.
  2. For each cycling activity without streams, fetch /activities/{id}/streams
     and store JSON in data/streams/{id}.json.

Safe to re-run: idempotent on Strava activity id, resumes where it left off.
Respects rate limits via headers from each response.

Usage:
    python backfill.py                # summaries + cycling streams
    python backfill.py --summaries    # only paginate summaries (fast, cheap)
    python backfill.py --streams      # only pull streams for known rides
    python backfill.py --all-sports   # don't filter to cycling for streams
    python backfill.py --details      # also pull full /activities/{id} detail
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from typing import Any

import storage
from strava import StravaClient

CYCLING_SPORT_TYPES = {
    "Ride",
    "VirtualRide",
    "MountainBikeRide",
    "GravelRide",
    "EBikeRide",
    "EMountainBikeRide",
    "Velomobile",
    "Handcycle",
}

STREAM_KEYS = (
    "time,distance,latlng,altitude,velocity_smooth,heartrate,cadence,watts,temp,"
    "moving,grade_smooth"
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def backfill_summaries(client: StravaClient) -> int:
    conn = storage.connect()
    total_new = 0
    page = 1
    per_page = 200
    while True:
        print(f"[summaries] page {page} (per_page={per_page})  "
              f"15min={client.usage_15min}/{client.limit_15min}  "
              f"daily={client.usage_daily}/{client.limit_daily}")
        batch = client.get("/athlete/activities", {"per_page": per_page, "page": page})
        if not batch:
            break
        existing = storage.known_ids(conn)
        new = 0
        for act in batch:
            if act["id"] not in existing:
                new += 1
            storage.upsert_summary(conn, act)
        conn.commit()
        total_new += new
        print(f"           +{new} new ({len(batch)} fetched, {len(existing)} previously known)")
        if len(batch) < per_page:
            break
        page += 1
    conn.close()
    print(f"[summaries] done. {total_new} new activity summaries.")
    return total_new


def backfill_streams(client: StravaClient, sport_filter: set[str] | None) -> int:
    conn = storage.connect()
    ids = storage.ids_missing_streams(
        conn, sport_types=sport_filter if sport_filter else None
    )
    print(f"[streams] {len(ids)} activities missing streams")
    if not ids:
        conn.close()
        return 0
    pulled = 0
    for i, aid in enumerate(ids, 1):
        try:
            data = client.get(
                f"/activities/{aid}/streams",
                {"keys": STREAM_KEYS, "key_by_type": "true"},
            )
        except Exception as e:
            print(f"[streams] {aid}: failed ({e}); skipping")
            continue
        storage.save_streams(aid, data)
        storage.mark_streams_fetched(conn, aid, now_iso())
        conn.commit()
        pulled += 1
        if i % 10 == 0 or i == len(ids):
            print(
                f"[streams] {i}/{len(ids)}  pulled={pulled}  "
                f"15min={client.usage_15min}/{client.limit_15min}  "
                f"daily={client.usage_daily}/{client.limit_daily}"
            )
    conn.close()
    return pulled


def backfill_details(client: StravaClient, sport_filter: set[str] | None) -> int:
    """Pull full /activities/{id} for any ride that only has summary data."""
    conn = storage.connect()
    if sport_filter:
        marks = ",".join("?" * len(sport_filter))
        rows = conn.execute(
            f"SELECT id FROM activities WHERE detail_fetched_at IS NULL "
            f"AND sport_type IN ({marks}) ORDER BY start_date DESC",
            tuple(sport_filter),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM activities WHERE detail_fetched_at IS NULL ORDER BY start_date DESC"
        ).fetchall()
    ids = [r[0] for r in rows]
    print(f"[details] {len(ids)} activities missing detail")
    pulled = 0
    for i, aid in enumerate(ids, 1):
        try:
            detail = client.get(f"/activities/{aid}", {"include_all_efforts": "false"})
        except Exception as e:
            print(f"[details] {aid}: failed ({e}); skipping")
            continue
        storage.upsert_detail(conn, detail, now_iso())
        conn.commit()
        pulled += 1
        if i % 10 == 0 or i == len(ids):
            print(
                f"[details] {i}/{len(ids)}  pulled={pulled}  "
                f"15min={client.usage_15min}/{client.limit_15min}"
            )
    conn.close()
    return pulled


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--summaries", action="store_true", help="Only paginate /athlete/activities")
    p.add_argument("--streams", action="store_true", help="Only pull missing streams")
    p.add_argument("--details", action="store_true", help="Also pull /activities/{id} detail")
    p.add_argument("--all-sports", action="store_true", help="Don't restrict streams to cycling")
    args = p.parse_args()

    client = StravaClient()
    sport_filter = None if args.all_sports else CYCLING_SPORT_TYPES

    if args.summaries:
        backfill_summaries(client)
        return
    if args.streams:
        backfill_streams(client, sport_filter)
        return
    if args.details:
        backfill_details(client, sport_filter)
        return

    # Default: summaries then streams.
    backfill_summaries(client)
    backfill_streams(client, sport_filter)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[backfill] interrupted; safe to re-run.")
        sys.exit(130)
