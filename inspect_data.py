"""Quick sanity check: print summary stats from the local store.

Usage: python inspect_data.py
"""
from __future__ import annotations

import json

import storage


def main() -> None:
    conn = storage.connect()
    total = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    by_sport = conn.execute(
        "SELECT sport_type, COUNT(*) FROM activities GROUP BY sport_type ORDER BY 2 DESC"
    ).fetchall()
    cycling_with_streams = conn.execute(
        "SELECT COUNT(*) FROM activities WHERE streams_fetched_at IS NOT NULL"
    ).fetchone()[0]
    earliest = conn.execute(
        "SELECT MIN(start_date), MAX(start_date) FROM activities"
    ).fetchone()
    power_rides = conn.execute(
        "SELECT COUNT(*) FROM activities WHERE device_watts = 1"
    ).fetchone()[0]

    print(f"Total activities      : {total}")
    print(f"With streams pulled   : {cycling_with_streams}")
    print(f"With real power meter : {power_rides}")
    print(f"Date range            : {earliest[0]} → {earliest[1]}")
    print("\nBy sport_type:")
    for st, n in by_sport:
        print(f"  {st or '(null)':<25} {n}")

    last_sync = storage.get_state(conn, "last_sync_iso")
    if last_sync:
        print(f"\nLast sync.py run: {last_sync}")

    # Show 3 most-recent rides
    print("\n5 most-recent activities:")
    rows = conn.execute(
        "SELECT start_date_local, sport_type, name, ROUND(distance/1000.0, 1) AS km, "
        "moving_time/60 AS mins, average_watts "
        "FROM activities ORDER BY start_date DESC LIMIT 5"
    ).fetchall()
    for r in rows:
        print(f"  {r[0]}  {r[1]:<12}  {r[3]:>6} km  {r[4]:>4} min  "
              f"{(str(int(r[5])) + 'W') if r[5] else '   -':>5}  {r[2]}")

    conn.close()


if __name__ == "__main__":
    main()
