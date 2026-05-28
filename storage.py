"""SQLite + JSON-on-disk storage for Strava data — multi-user edition.

Layout:
    data/rides.db                          SQLite (users, activities, sessions, sync_state)
    data/streams/{user_id}/{aid}.json      Per-user per-ride time-series

All activity reads/writes take a user_id. The legacy single-user code path
(no user_id arg) is no longer supported — run `migrate_to_multiuser.py`
once to upgrade an existing database.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STREAMS_DIR = DATA_DIR / "streams"
DB_PATH = DATA_DIR / "rides.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    strava_athlete_id    INTEGER UNIQUE NOT NULL,
    email                TEXT,
    first_name           TEXT,
    last_name            TEXT,
    profile_image_url    TEXT,
    sex                  TEXT,              -- 'M' or 'F' (from Strava)
    age                  INTEGER,           -- user-provided (Strava doesn't expose DOB)
    weight_kg            REAL,
    refresh_token        TEXT NOT NULL,
    access_token         TEXT,
    access_token_expires INTEGER,           -- unix seconds
    last_sync_at         TEXT,              -- ISO8601
    backfill_state       TEXT NOT NULL DEFAULT 'pending',   -- pending|running|done|failed
    backfill_progress    INTEGER NOT NULL DEFAULT 0,
    backfill_total       INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    deauthorized_at      TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    user_id              INTEGER NOT NULL,
    id                   INTEGER NOT NULL,
    name                 TEXT,
    sport_type           TEXT,
    type                 TEXT,
    start_date           TEXT,
    start_date_local     TEXT,
    timezone             TEXT,
    distance             REAL,
    moving_time          INTEGER,
    elapsed_time         INTEGER,
    total_elevation_gain REAL,
    average_speed        REAL,
    max_speed            REAL,
    average_watts        REAL,
    max_watts            INTEGER,
    weighted_average_watts INTEGER,
    kilojoules           REAL,
    device_watts         INTEGER,
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
    detail_fetched_at    TEXT,
    streams_fetched_at   TEXT,
    raw                  TEXT NOT NULL,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_activities_user_start ON activities(user_id, start_date DESC);
CREATE INDEX IF NOT EXISTS idx_activities_user_sport ON activities(user_id, sport_type);

CREATE TABLE IF NOT EXISTS sync_state (
    user_id              INTEGER NOT NULL,
    key                  TEXT NOT NULL,
    value                TEXT,
    PRIMARY KEY (user_id, key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sessions (
    sid                  TEXT PRIMARY KEY,
    user_id              INTEGER NOT NULL,
    expires_at           INTEGER NOT NULL,         -- unix seconds
    created_at           INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
"""


# =====================================================================
#                          CONNECTION
# =====================================================================

def _ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    STREAMS_DIR.mkdir(exist_ok=True)


def connect() -> sqlite3.Connection:
    _ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL: allows concurrent readers + one writer. With background workers
    # plus the Flask request thread hitting the DB, the default rollback
    # journal would serialise too aggressively.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")  # fast + still durable in WAL
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE for columns added after initial schema.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, so we introspect pragma table_info
    and add anything missing. Safe to run on every connect().
    """
    needed = {
        "users": [("sex", "TEXT"), ("age", "INTEGER")],
    }
    for table, cols in needed.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, sqltype in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")
    conn.commit()


def streams_dir_for(user_id: int) -> Path:
    _ensure_dirs()
    d = STREAMS_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


# =====================================================================
#                            USERS
# =====================================================================

@dataclass
class User:
    id: int
    strava_athlete_id: int
    email: str | None
    first_name: str | None
    last_name: str | None
    profile_image_url: str | None
    sex: str | None              # 'M' / 'F' from Strava
    age: int | None              # user-provided
    weight_kg: float | None
    refresh_token: str
    access_token: str | None
    access_token_expires: int | None
    last_sync_at: str | None
    backfill_state: str
    backfill_progress: int
    backfill_total: int
    created_at: str
    deauthorized_at: str | None

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or f"Athlete {self.strava_athlete_id}"


def _user_from_row(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(**{k: row[k] for k in row.keys()})


def get_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_from_row(row)


def get_user_by_athlete_id(conn: sqlite3.Connection, athlete_id: int) -> User | None:
    row = conn.execute(
        "SELECT * FROM users WHERE strava_athlete_id = ?", (athlete_id,)
    ).fetchone()
    return _user_from_row(row)


def upsert_user_from_oauth(
    conn: sqlite3.Connection,
    athlete: dict[str, Any],
    refresh_token: str,
    access_token: str,
    access_token_expires: int,
) -> User:
    """Insert or update a user from a Strava /oauth/token response.

    Strava returns the athlete payload alongside the tokens. We pull just
    the identifying / display fields.
    """
    athlete_id = int(athlete["id"])
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    existing = get_user_by_athlete_id(conn, athlete_id)
    sex = athlete.get("sex")
    if sex not in ("M", "F"):
        sex = None
    weight = athlete.get("weight")  # kg or None
    if existing:
        # On re-sign-in, update everything Strava knows about EXCEPT fields the
        # user might have customised locally (weight — only refresh if it's not
        # locally set; sex — Strava is authoritative).
        update_weight = weight if existing.weight_kg is None else existing.weight_kg
        conn.execute(
            "UPDATE users SET refresh_token = ?, access_token = ?, "
            "access_token_expires = ?, first_name = ?, last_name = ?, "
            "profile_image_url = ?, sex = COALESCE(?, sex), weight_kg = ?, "
            "deauthorized_at = NULL WHERE id = ?",
            (refresh_token, access_token, access_token_expires,
             athlete.get("firstname"), athlete.get("lastname"),
             athlete.get("profile") or athlete.get("profile_medium"),
             sex, update_weight, existing.id),
        )
        conn.commit()
        return get_user(conn, existing.id)  # type: ignore[return-value]
    cur = conn.execute(
        "INSERT INTO users (strava_athlete_id, email, first_name, last_name, "
        "profile_image_url, sex, weight_kg, refresh_token, access_token, "
        "access_token_expires, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (athlete_id, athlete.get("email"),
         athlete.get("firstname"), athlete.get("lastname"),
         athlete.get("profile") or athlete.get("profile_medium"),
         sex, weight, refresh_token, access_token,
         access_token_expires, now_iso),
    )
    conn.commit()
    return get_user(conn, int(cur.lastrowid))  # type: ignore[return-value]


def update_user_tokens(
    conn: sqlite3.Connection, user_id: int,
    refresh_token: str, access_token: str, access_token_expires: int,
) -> None:
    conn.execute(
        "UPDATE users SET refresh_token = ?, access_token = ?, "
        "access_token_expires = ? WHERE id = ?",
        (refresh_token, access_token, access_token_expires, user_id),
    )
    conn.commit()


def update_backfill_state(
    conn: sqlite3.Connection, user_id: int,
    state: str | None = None,
    progress: int | None = None, total: int | None = None,
) -> None:
    fields, vals = [], []
    if state is not None:
        fields.append("backfill_state = ?"); vals.append(state)
    if progress is not None:
        fields.append("backfill_progress = ?"); vals.append(progress)
    if total is not None:
        fields.append("backfill_total = ?"); vals.append(total)
    if not fields:
        return
    vals.append(user_id)
    conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", vals)
    conn.commit()


def set_user_weight(conn: sqlite3.Connection, user_id: int, weight_kg: float) -> None:
    conn.execute("UPDATE users SET weight_kg = ? WHERE id = ?", (weight_kg, user_id))
    conn.commit()


def set_user_demographics(
    conn: sqlite3.Connection, user_id: int,
    sex: str | None = None, age: int | None = None,
    weight_kg: float | None = None,
) -> None:
    """Update any subset of {sex, age, weight_kg} on the user record."""
    fields, vals = [], []
    if sex is not None and sex in ("M", "F"):
        fields.append("sex = ?"); vals.append(sex)
    if age is not None:
        fields.append("age = ?"); vals.append(int(age))
    if weight_kg is not None:
        fields.append("weight_kg = ?"); vals.append(float(weight_kg))
    if not fields:
        return
    vals.append(user_id)
    conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", vals)
    conn.commit()


# =====================================================================
#                            SESSIONS
# =====================================================================

SESSION_LIFETIME_S = 60 * 60 * 24 * 30  # 30 days


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute(
        "INSERT INTO sessions (sid, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (sid, user_id, now + SESSION_LIFETIME_S, now),
    )
    conn.commit()
    return sid


def lookup_session(conn: sqlite3.Connection, sid: str) -> User | None:
    if not sid:
        return None
    now = int(time.time())
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.sid = ? AND s.expires_at > ?",
        (sid, now),
    ).fetchone()
    return _user_from_row(row)


def destroy_session(conn: sqlite3.Connection, sid: str) -> None:
    conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
    conn.commit()


def cleanup_expired_sessions(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
    conn.commit()
    return cur.rowcount


# =====================================================================
#                          ACTIVITIES
# =====================================================================

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


def upsert_summary(conn: sqlite3.Connection, user_id: int, summary: dict[str, Any]) -> None:
    row = _extract(summary)
    row["raw"] = json.dumps(summary)
    row["user_id"] = user_id
    cols = list(row.keys())
    placeholders = ",".join(f":{c}" for c in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("id", "user_id"))
    conn.execute(
        f"INSERT INTO activities ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(user_id, id) DO UPDATE SET {updates}",
        row,
    )


def upsert_detail(
    conn: sqlite3.Connection, user_id: int, detail: dict[str, Any], now_iso: str
) -> None:
    row = _extract(detail)
    row["raw"] = json.dumps(detail)
    row["detail_fetched_at"] = now_iso
    row["user_id"] = user_id
    cols = list(row.keys())
    placeholders = ",".join(f":{c}" for c in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("id", "user_id"))
    conn.execute(
        f"INSERT INTO activities ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(user_id, id) DO UPDATE SET {updates}",
        row,
    )


def mark_streams_fetched(
    conn: sqlite3.Connection, user_id: int, activity_id: int, now_iso: str,
) -> None:
    conn.execute(
        "UPDATE activities SET streams_fetched_at = ? WHERE user_id = ? AND id = ?",
        (now_iso, user_id, activity_id),
    )


def save_streams(user_id: int, activity_id: int, streams_obj: dict[str, Any]) -> Path:
    p = streams_dir_for(user_id) / f"{activity_id}.json"
    p.write_text(json.dumps(streams_obj))
    return p


def streams_path(user_id: int, activity_id: int) -> Path:
    return STREAMS_DIR / str(user_id) / f"{activity_id}.json"


def known_ids(conn: sqlite3.Connection, user_id: int) -> set[int]:
    return {r[0] for r in conn.execute(
        "SELECT id FROM activities WHERE user_id = ?", (user_id,))}


def ids_missing_streams(
    conn: sqlite3.Connection, user_id: int,
    sport_types: Iterable[str] | None = None,
) -> list[int]:
    if sport_types:
        sport_list = list(sport_types)
        marks = ",".join("?" * len(sport_list))
        rows = conn.execute(
            f"SELECT id FROM activities WHERE user_id = ? AND streams_fetched_at IS NULL "
            f"AND sport_type IN ({marks}) ORDER BY start_date DESC",
            (user_id, *sport_list),
        )
    else:
        rows = conn.execute(
            "SELECT id FROM activities WHERE user_id = ? AND streams_fetched_at IS NULL "
            "ORDER BY start_date DESC",
            (user_id,),
        )
    return [r[0] for r in rows]


# =====================================================================
#                         SYNC STATE (per user)
# =====================================================================

def get_state(conn: sqlite3.Connection, user_id: int, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM sync_state WHERE user_id = ? AND key = ?",
        (user_id, key),
    ).fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, user_id: int, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(user_id, key, value) VALUES(?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
        (user_id, key, value),
    )
    conn.commit()
