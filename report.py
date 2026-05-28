"""Generate a quick performance report from local Strava data.

Produces:
  reports/power_duration_curve.png
  reports/weekly_volume.png
  reports/summary.txt           text rollup (FTP estimate, totals, recent rides)

Usage:
  python report.py                       # all data
  python report.py --since 2025-01-01    # filter by date
  python report.py --ftp 250             # override the FTP estimate
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import analysis

ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "reports"


def fmt_secs(d: int) -> str:
    if d < 60: return f"{d}s"
    if d < 3600: return f"{d // 60}m" + (f"{d % 60}s" if d % 60 else "")
    h, rem = divmod(d, 3600); m = rem // 60
    return f"{h}h" + (f"{m}m" if m else "")


def plot_pdc(pdc: pd.DataFrame, out: Path) -> None:
    valid = pdc.dropna()
    if valid.empty:
        return
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(valid.index, valid["watts"], marker="o", linewidth=2)
    ax.set_xscale("log")
    ax.set_xlabel("Duration (seconds, log scale)")
    ax.set_ylabel("Best mean power (watts)")
    ax.set_title("Power-Duration Curve — all-time best")
    ax.grid(True, which="both", alpha=0.3)
    ticks = [1, 5, 15, 60, 300, 1200, 3600, 14400]
    ax.set_xticks(ticks)
    ax.set_xticklabels([fmt_secs(t) for t in ticks])
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)


def plot_weekly(weekly: pd.DataFrame, out: Path) -> None:
    if weekly.empty:
        return
    fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
    axes[0].bar(weekly.index, weekly["hours"], width=5, color="#fc4c02")
    axes[0].set_ylabel("Hours")
    axes[0].set_title("Weekly volume")
    axes[1].bar(weekly.index, weekly["distance_km"], width=5, color="#444")
    axes[1].set_ylabel("Distance (km)")
    if "tss" in weekly.columns and weekly["tss"].notna().any():
        axes[2].bar(weekly.index, weekly["tss"], width=5, color="#0a8")
        axes[2].set_ylabel("TSS")
    else:
        axes[2].bar(weekly.index, weekly["elevation_m"], width=5, color="#0a8")
        axes[2].set_ylabel("Elevation (m)")
    for ax in axes:
        ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--user-id", type=int, default=1)
    p.add_argument("--since", help="ISO date filter (start_date >= this)")
    p.add_argument("--ftp", type=float, help="Override FTP estimate (W)")
    args = p.parse_args()
    REPORTS_DIR.mkdir(exist_ok=True)

    df = analysis.load_rides(user_id=args.user_id, only_cycling=True)
    if args.since:
        df = df[df["start_date"] >= pd.Timestamp(args.since, tz="UTC")]
    n_total = len(df)
    n_power = int((df["device_watts"] == 1).sum())
    n_streams = int(df["streams_fetched_at"].notna().sum())

    power_ids = df[df["device_watts"] == 1]["id"].astype(int).tolist()
    streams_root = analysis.STREAMS_DIR / str(args.user_id)
    power_ids_with_streams = [
        aid for aid in power_ids
        if (streams_root / f"{aid}.json").exists()
        or (analysis.STREAMS_DIR / f"{aid}.json").exists()  # legacy fallback
    ]

    pdc = (analysis.power_duration_curve(args.user_id, power_ids_with_streams)
           if power_ids_with_streams else pd.DataFrame())
    ftp = args.ftp if args.ftp else (analysis.estimate_ftp(pdc) if not pdc.empty else float("nan"))

    if not pdc.empty:
        plot_pdc(pdc, REPORTS_DIR / "power_duration_curve.png")

    df_for_tss = df.copy()
    if not np.isnan(ftp) and n_streams > 0:
        df_for_tss = analysis.add_tss_columns(df_for_tss, ftp=ftp, user_id=args.user_id)
    weekly = analysis.weekly_summary(df_for_tss)
    plot_weekly(weekly, REPORTS_DIR / "weekly_volume.png")

    summary_lines = []
    summary_lines.append("Wilson's AI Coach — performance report")
    summary_lines.append("=" * 50)
    summary_lines.append(f"Generated: {dt.datetime.now().isoformat(timespec='seconds')}")
    if not df.empty:
        summary_lines.append(
            f"Range:     {df['start_date'].min().date()} → {df['start_date'].max().date()}"
        )
    summary_lines.append(f"Rides:     {n_total}  (with real power meter: {n_power}, with streams: {n_streams})")
    summary_lines.append(f"Total km:  {df['distance'].sum() / 1000:.0f}")
    summary_lines.append(f"Total hr:  {df['moving_time'].sum() / 3600:.0f}")
    summary_lines.append(f"Total m climbed: {df['total_elevation_gain'].sum():.0f}")
    summary_lines.append("")
    if not np.isnan(ftp):
        summary_lines.append(f"FTP estimate (20-min × 0.95): {ftp:.0f} W")
    else:
        summary_lines.append("FTP estimate: insufficient streams data yet")

    if not pdc.empty:
        summary_lines.append("")
        summary_lines.append("Power-duration curve (best mean-max):")
        for d, row in pdc.iterrows():
            if pd.isna(row["watts"]):
                continue
            summary_lines.append(f"  {fmt_secs(int(d)):>6}  {row['watts']:5.0f} W   ride {int(row['activity_id'])}")

    if not weekly.empty:
        last4 = weekly.tail(4)
        summary_lines.append("")
        summary_lines.append("Last 4 weeks:")
        for week, r in last4.iterrows():
            line = f"  {week.date()}  rides={int(r['rides']):2d}  hrs={r['hours']:4.1f}  km={r['distance_km']:5.0f}  elev={int(r['elevation_m']):4d}m"
            if "tss" in last4.columns and not pd.isna(r["tss"]):
                line += f"  TSS={r['tss']:.0f}"
            summary_lines.append(line)

    out_txt = REPORTS_DIR / "summary.txt"
    out_txt.write_text("\n".join(summary_lines) + "\n")
    print("\n".join(summary_lines))
    print(f"\nWrote {out_txt}")
    if not pdc.empty:
        print(f"Wrote {REPORTS_DIR / 'power_duration_curve.png'}")
    if not weekly.empty:
        print(f"Wrote {REPORTS_DIR / 'weekly_volume.png'}")


if __name__ == "__main__":
    main()
