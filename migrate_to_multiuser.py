"""One-shot migration from the single-user SQLite schema to the multi-user one.

What it does:
  1. Backs up data/rides.db → data/rides.db.bak-<timestamp>
  2. Reads the old `activities` and `sync_state` tables (single-user schema).
  3. Drops + recreates the schema with users / sessions / multi-tenant tables.
  4. Inserts the local user (from .env: STRAVA_CLIENT_ID + STRAVA_REFRESH_TOKEN)
     as user_id=1, calling Strava once to fetch the athlete payload.
  5. Re-inserts all activities and sync_state stamped with user_id=1.
  6. Moves stream files: data/streams/{aid}.json → data/streams/1/{aid}.json.

Idempotent: if it sees the new schema (users table) already populated, it
exits cleanly.

Run once:
    source .venv/bin/activate
    python migrate_to_multiuser.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import storage

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
DB_PATH = storage.DB_PATH
STREAMS_DIR = storage.STREAMS_DIR


def _backup(db: Path) -> Path:
    if not db.exists():
        return db
    bak = db.with_suffix(db.suffix + f".bak-{int(time.time())}")
    shutil.copy2(db, bak)
    print(f"[migrate] backed up {db} → {bak}")
    return bak


def _has_old_schema(conn: sqlite3.Connection) -> bool:
    """True if the DB has the old single-user activities schema (no user_id)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(activities)").fetchall()]
    return bool(cols) and "user_id" not in cols


def _has_new_schema_with_user(conn: sqlite3.Connection) -> bool:
    try:
        n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return n > 0
    except sqlite3.OperationalError:
        return False


def _refresh_strava_token(client_id: str, client_secret: str, refresh_token: str):
    """Get a fresh access token + athlete payload using the refresh token."""
    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _get_athlete(access_token: str) -> dict:
    resp = requests.get(
        "https://www.strava.com/api/v3/athlete",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    if not ENV_PATH.exists():
        sys.exit(f"Missing {ENV_PATH}. Need STRAVA_CLIENT_ID/SECRET/REFRESH_TOKEN for the existing user.")
    load_dotenv(ENV_PATH)
    client_id = os.environ.get("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN", "").strip()
    if not (client_id and client_secret and refresh_token):
        sys.exit("STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN all required in .env")

    if not DB_PATH.exists():
        print("[migrate] no existing data/rides.db — running fresh schema init only.")
        conn = storage.connect()
        conn.close()
        print("[migrate] done (empty database). Sign in via /login when the web app starts.")
        return

    # Read old schema content first
    raw_conn = sqlite3.connect(DB_PATH)
    raw_conn.row_factory = sqlite3.Row
    if _has_new_schema_with_user(raw_conn):
        print("[migrate] users table is already populated. Nothing to do.")
        raw_conn.close()
        return
    if not _has_old_schema(raw_conn):
        # Empty DB or already-new but no users — just init schema
        raw_conn.close()
        conn = storage.connect()
        conn.close()
        print("[migrate] initialised new schema (no data to migrate).")
        return

    # Dump old data
    old_activities = [dict(r) for r in raw_conn.execute("SELECT * FROM activities").fetchall()]
    try:
        old_sync = [dict(r) for r in raw_conn.execute("SELECT * FROM sync_state").fetchall()]
    except sqlite3.OperationalError:
        old_sync = []
    raw_conn.close()

    print(f"[migrate] dumped {len(old_activities)} activities, {len(old_sync)} sync_state rows.")

    # Refresh token + grab athlete info BEFORE destroying the DB
    print("[migrate] refreshing Strava token and fetching athlete profile...")
    tok = _refresh_strava_token(client_id, client_secret, refresh_token)
    athlete = _get_athlete(tok["access_token"])
    athlete_id = athlete["id"]
    print(f"[migrate] athlete: {athlete.get('firstname','')} {athlete.get('lastname','')} (id={athlete_id})")

    # Back up and drop-recreate
    _backup(DB_PATH)
    raw_conn = sqlite3.connect(DB_PATH)
    raw_conn.executescript("""
        DROP TABLE IF EXISTS activities;
        DROP TABLE IF EXISTS sync_state;
    """)
    raw_conn.commit()
    raw_conn.close()

    # New schema + insert user
    conn = storage.connect()
    user = storage.upsert_user_from_oauth(
        conn, athlete,
        refresh_token=tok["refresh_token"],
        access_token=tok["access_token"],
        access_token_expires=int(tok["expires_at"]),
    )
    # Migrated user has data already; mark backfill done
    storage.update_backfill_state(conn, user.id, state="done",
                                   progress=len(old_activities), total=len(old_activities))
    print(f"[migrate] inserted user_id={user.id} for athlete {athlete_id}.")

    # Insert activities — re-stamp with user_id
    n_inserted = 0
    for act in old_activities:
        # Drop the legacy `id` PRIMARY KEY conflict by composing the new row
        act["user_id"] = user.id
        cols = [c for c in act.keys() if c != "raw"]  # raw stays as-is
        # We have all the columns from the old schema; the new schema has the
        # same set plus user_id (added) and the (user_id, id) PK.
        cols_with_raw = list(act.keys())
        placeholders = ",".join(f":{c}" for c in cols_with_raw)
        conn.execute(
            f"INSERT INTO activities ({','.join(cols_with_raw)}) VALUES ({placeholders})",
            act,
        )
        n_inserted += 1
    conn.commit()
    print(f"[migrate] inserted {n_inserted} activities.")

    # sync_state
    for row in old_sync:
        storage.set_state(conn, user.id, row["key"], row["value"])
    print(f"[migrate] inserted {len(old_sync)} sync_state rows.")

    # Save the refreshed token also to .env (in case anyone still uses the CLI)
    from strava import write_env_value
    write_env_value("STRAVA_REFRESH_TOKEN", tok["refresh_token"])

    conn.close()

    # Move stream files
    user_streams_dir = storage.streams_dir_for(user.id)
    moved = 0
    for f in STREAMS_DIR.iterdir():
        if f.is_file() and f.suffix == ".json":
            f.rename(user_streams_dir / f.name)
            moved += 1
    print(f"[migrate] moved {moved} stream files into {user_streams_dir}.")
    print("\n[migrate] done. You can now run `python app.py` and sign in via /login.")


if __name__ == "__main__":
    main()
