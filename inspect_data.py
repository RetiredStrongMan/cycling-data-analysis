"""Quick sanity check: print summary stats from the local store.

Usage:
    python inspect_data.py                  # all users
    python inspect_data.py --user-id 1      # one user only
"""
from __future__ import annotations

import argparse

import storage


def show_for_user(conn, user_id: int, user_label: str) -> None:
    print(f"\n=== user_id={user_id}  ({user_label}) ===")
    total = conn.execute(
        "SELECT COUNT(*) FROM activities WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    by_sport = conn.execute(
        "SELECT sport_type, COUNT(*) FROM activities WHERE user_id = ? "
        "GROUP BY sport_type ORDER BY 2 DESC", (user_id,)
    ).fetchall()
    cycling_with_streams = conn.execute(
        "SELECT COUNT(*) FROM activities WHERE user_id = ? "
        "AND streams_fetched_at IS NOT NULL", (user_id,)
    ).fetchone()[0]
    earliest = conn.execute(
        "SELECT MIN(start_date), MAX(start_date) FROM activities WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    power_rides = conn.execute(
        "SELECT COUNT(*) FROM activities WHERE user_id = ? AND device_watts = 1",
        (user_id,),
    ).fetchone()[0]

    print(f"Total activities      : {total}")
    print(f"With streams pulled   : {cycling_with_streams}")
    print(f"With real power meter : {power_rides}")
    print(f"Date range            : {earliest[0]} → {earliest[1]}")
    if by_sport:
        print("By sport_type:")
        for st, n in by_sport:
            print(f"  {st or '(null)':<25} {n}")

    last_sync = storage.get_state(conn, user_id, "last_sync_iso")
    if last_sync:
        print(f"Last sync.py run: {last_sync}")

    print("\n5 most-recent activities:")
    rows = conn.execute(
        "SELECT start_date_local, sport_type, name, ROUND(distance/1000.0, 1) AS km, "
        "moving_time/60 AS mins, average_watts "
        "FROM activities WHERE user_id = ? ORDER BY start_date DESC LIMIT 5",
        (user_id,),
    ).fetchall()
    for r in rows:
        print(f"  {r[0]}  {r[1]:<12}  {r[3]:>6} km  {r[4]:>4} min  "
              f"{(str(int(r[5])) + 'W') if r[5] else '   -':>5}  {r[2]}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--user-id", type=int, default=None,
                   help="Restrict to one user_id; omit to scan all users.")
    args = p.parse_args()

    conn = storage.connect()
    if args.user_id is None:
        users = conn.execute(
            "SELECT id, first_name, last_name, strava_athlete_id FROM users ORDER BY id"
        ).fetchall()
        if not users:
            print("No users yet. Sign in via the web app, or run migrate_to_multiuser.py.")
        for u in users:
            label = f"{u[1] or ''} {u[2] or ''} (athlete {u[3]})".strip()
            show_for_user(conn, u[0], label)
    else:
        u = conn.execute(
            "SELECT first_name, last_name, strava_athlete_id FROM users WHERE id = ?",
            (args.user_id,),
        ).fetchone()
        if not u:
            print(f"No user with id={args.user_id}")
        else:
            label = f"{u[0] or ''} {u[1] or ''} (athlete {u[2]})".strip()
            show_for_user(conn, args.user_id, label)
    conn.close()


if __name__ == "__main__":
    main()
