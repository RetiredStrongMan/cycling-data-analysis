"""SQLite + JSON-on-disk storage for Strava data.

Layout:
    data/rides.db            SQLite with activity summary + detail.
    data/streams/{id}.json   Per-ride time-series (one file per activity).

Schema is intentionally permissive: we store the full JSON for each ride alongside
extracted scalar columns we'll query often. New fields don't require migrations —
just read from the `raw` column.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STREAMS_DIR = DATA_DIR / "streams"
DB_PATH = DATA_DIR / "rides.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT,
    sport_type           TEXT,
    type                 TEXT,
    start_date           TEXT,        -- ISO8601 UTC
    start_date_local     TEXT,
    timezone             TEXT,
    distance             REAL,        -- meters
    moving_time          INTEGER,     -- seconds
    elapsed_time         INTEGER,
    total_elevation_gain REAL,        -- meters
    average_speed        REAL,        -- m/s
    max_speed            REAL,
    average_watts        REAL,
    max_watts            INTEGER,
    weighted_average_watts INTEGER,   -- NP (detail-only)
    kilojoules           REAL,
    device_watts         INTEGER,     -- 0/1: true power meter
    has_heartrate        INTEGER,
    average_heartrate    REAL,
    max_heartrate        REAL,
    suffer_score         INTEGER,
    trainer              INTEGER,
    commute              INTEGER,
    manual               INTEGER,
    private              INTEGER,
    gear_id              TEXT,
    polyline             TEXT,
    summary_polyline     TEXT,
    detail_fetched_at    TEXT,        -- ISO8601; NULL means only summary so far
    streams_fetched_at   TEXT,        -- ISO8601; NULL means streams not pulled
    raw                  TEXT NOT NULL  -- full JSON (summary or detail)
);
CREATE INDEX IF NOT EXISTS idx_activities_start_date ON activities(start_date);
CREATE INDEX IF NOT EXISTS idx_activities_sport_type ON activities(sport_type);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    STREAMS_DIR.mkdir(exist_ok=True)


def connect() -> sqlite3.Connection:
    _ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


SUMMARY_COLS = (
    "id name sport_type type start_date start_date_local timezone distance "
    "moving_time elapsed_time total_elevation_gain average_speed max_speed "
    "average_watts max_watts kilojoules device_watts has_heartrate "
    "average_heartrate max_heartrate suffer_score trainer commute manual private gear_id"
).split()


def _extract(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in SUMMARY_COLS:
        v = d.get(col)
        if isinstance(v, bool):
            v = 1 if v else 0
        out[col] = v
    out["polyline"] = (d.get("map") or {}).get("polyline")
    out["summary_polyline"] = (d.get("map") or {}).get("summary_polyline")
    out["weighted_average_watts"] = d.get("weighted_average_watts")
    return out


def upsert_summary(conn: sqlite3.Connection, summary: dict[str, Any]) -> None:
    row = _extract(summary)
    row["raw"] = json.dumps(summary)
    cols = list(row.keys())
    placeholders = ",".join(f":{c}" for c in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    conn.execute(
        f"INSERT INTO activities ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        row,
    )


def upsert_detail(conn: sqlite3.Connection, detail: dict[str, Any], now_iso: str) -> None:
    row = _extract(detail)
    row["raw"] = json.dumps(detail)
    row["detail_fetched_at"] = now_iso
    cols = list(row.keys())
    placeholders = ",".join(f":{c}" for c in cols)
    # On conflict we merge: detail fields always win.
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    conn.execute(
        f"INSERT INTO activities ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        row,
    )


def mark_streams_fetched(conn: sqlite3.Connection, activity_id: int, now_iso: str) -> None:
    conn.execute(
        "UPDATE activities SET streams_fetched_at = ? WHERE id = ?",
        (now_iso, activity_id),
    )


def save_streams(activity_id: int, streams_obj: dict[str, Any]) -> Path:
    _ensure_dirs()
    p = STREAMS_DIR / f"{activity_id}.json"
    p.write_text(json.dumps(streams_obj))
    return p


def known_ids(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT id FROM activities")}


def ids_missing_streams(
    conn: sqlite3.Connection, sport_types: Iterable[str] | None = None
) -> list[int]:
    if sport_types:
        marks = ",".join("?" * len(list(sport_types)))
        rows = conn.execute(
            f"SELECT id FROM activities WHERE streams_fetched_at IS NULL "
            f"AND sport_type IN ({marks}) ORDER BY start_date DESC",
            tuple(sport_types),
        )
    else:
        rows = conn.execute(
            "SELECT id FROM activities WHERE streams_fetched_at IS NULL ORDER BY start_date DESC"
        )
    return [r[0] for r in rows]


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
