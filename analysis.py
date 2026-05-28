"""Cycling-performance analytics over the local Strava store.

All functions return pandas DataFrames / numpy arrays so they're easy to chain in a
notebook. CLI usage is in `report.py`.

Key functions:
  load_rides()                       all activities as a DataFrame
  load_streams(activity_id)          one ride's time-series as a dict of np arrays
  power_duration_curve(ids, durs)    best mean-max power across rides for each duration
  estimate_ftp(...)                  20-min × 0.95 estimate from PDC
  tss(np_watts, ftp, moving_time_s)  Training Stress Score per ride
  weekly_summary(df)                 distance/time/elev/TSS rolled up by week
  hr_decoupling(activity_id)         power-to-HR ratio drift across a single ride
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

import storage

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STREAMS_DIR = DATA_DIR / "streams"

CYCLING_SPORT_TYPES = {
    "Ride", "VirtualRide", "MountainBikeRide", "GravelRide", "EBikeRide",
    "EMountainBikeRide", "Velomobile", "Handcycle",
}

# Default PDC durations (seconds): 1s..4h, log-spaced
DEFAULT_DURATIONS = [
    1, 5, 10, 15, 30, 60, 120, 180, 300, 600, 900,
    1200, 1800, 2400, 3600, 5400, 7200, 10800, 14400,
]


# ----- Loading ------------------------------------------------------------

def load_rides(only_cycling: bool = True, only_with_power: bool = False) -> pd.DataFrame:
    conn = storage.connect()
    df = pd.read_sql("SELECT * FROM activities", conn, parse_dates=["start_date", "start_date_local"])
    conn.close()
    if only_cycling:
        df = df[df["sport_type"].isin(CYCLING_SPORT_TYPES)].copy()
    if only_with_power:
        df = df[df["device_watts"] == 1].copy()
    df = df.sort_values("start_date").reset_index(drop=True)
    return df


def load_streams(activity_id: int) -> dict[str, np.ndarray]:
    """Return per-key numpy arrays. Returns empty dict if no streams on disk."""
    p = STREAMS_DIR / f"{activity_id}.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    out: dict[str, np.ndarray] = {}
    # Strava streams come keyed by type when key_by_type=true. Each value:
    #   {"type": "watts", "data": [...], "series_type": "...", "original_size": N, "resolution": "high"}
    for key, payload in raw.items():
        data = payload.get("data")
        if data is None:
            continue
        out[key] = np.asarray(data)
    return out


def list_rides_with_streams(only_with_power: bool = False) -> list[int]:
    conn = storage.connect()
    q = "SELECT id FROM activities WHERE streams_fetched_at IS NOT NULL"
    if only_with_power:
        q += " AND device_watts = 1"
    q += " ORDER BY start_date"
    ids = [r[0] for r in conn.execute(q)]
    conn.close()
    return ids


# ----- Power-duration curve -----------------------------------------------

def best_mean_max(power: np.ndarray, durations: Iterable[int]) -> dict[int, float]:
    """For each duration (in seconds), return the highest rolling-mean power on this ride.

    Assumes power samples are 1Hz (Strava's default for streams).
    """
    out: dict[int, float] = {}
    if power.size == 0:
        return out
    # Replace NaNs with 0 — Strava sometimes sends nulls during stops
    p = np.nan_to_num(power.astype(float), nan=0.0)
    cumsum = np.concatenate(([0.0], np.cumsum(p)))
    for d in durations:
        if d > p.size:
            out[d] = float("nan")
            continue
        # Rolling sum of length d, vectorised
        window_sums = cumsum[d:] - cumsum[:-d]
        out[d] = float(window_sums.max() / d)
    return out


def power_duration_curve(
    activity_ids: Iterable[int] | None = None,
    durations: Iterable[int] = DEFAULT_DURATIONS,
) -> pd.DataFrame:
    """Best mean-max power across the given rides for each duration.

    Returns a DataFrame indexed by duration (seconds), columns: ['watts', 'activity_id'].
    """
    if activity_ids is None:
        activity_ids = list_rides_with_streams(only_with_power=True)
    durations = list(durations)
    best = {d: (float("-inf"), None) for d in durations}
    for aid in activity_ids:
        streams = load_streams(aid)
        watts = streams.get("watts")
        if watts is None or watts.size == 0:
            continue
        bmm = best_mean_max(watts, durations)
        for d, w in bmm.items():
            if np.isnan(w):
                continue
            if w > best[d][0]:
                best[d] = (w, aid)
    rows = [
        {"duration_s": d, "watts": (v if v != float("-inf") else float("nan")), "activity_id": aid}
        for d, (v, aid) in best.items()
    ]
    return pd.DataFrame(rows).set_index("duration_s")


def estimate_ftp(pdc: pd.DataFrame | None = None) -> float:
    """Common 20-min-best × 0.95 estimate. Returns FTP in watts."""
    if pdc is None:
        pdc = power_duration_curve()
    if 1200 not in pdc.index or pd.isna(pdc.loc[1200, "watts"]):
        return float("nan")
    return float(pdc.loc[1200, "watts"] * 0.95)


# ----- TSS, NP, IF --------------------------------------------------------

def normalized_power(watts: np.ndarray) -> float:
    """Standard NP: 30s rolling avg, raised to 4th, mean, 4th root."""
    p = np.nan_to_num(watts.astype(float), nan=0.0)
    if p.size < 30:
        return float("nan")
    # 30-second rolling mean
    cumsum = np.concatenate(([0.0], np.cumsum(p)))
    roll = (cumsum[30:] - cumsum[:-30]) / 30.0
    return float(np.power(np.mean(np.power(roll, 4)), 0.25))


def tss(np_watts: float, ftp: float, moving_time_s: float) -> float:
    if not (np_watts and ftp and moving_time_s) or np.isnan(np_watts) or ftp == 0:
        return float("nan")
    intensity_factor = np_watts / ftp
    return (moving_time_s * np_watts * intensity_factor) / (ftp * 3600.0) * 100.0


def add_tss_columns(df: pd.DataFrame, ftp: float) -> pd.DataFrame:
    """Add NP/IF/TSS columns. Prefers weighted_average_watts (from detail) when present;
    falls back to NP computed from streams; otherwise NaN."""
    df = df.copy()
    nps: list[float] = []
    for _, r in df.iterrows():
        np_w = r.get("weighted_average_watts")
        if pd.isna(np_w) or np_w is None:
            streams = load_streams(int(r["id"]))
            w = streams.get("watts")
            np_w = normalized_power(w) if w is not None else float("nan")
        nps.append(float(np_w) if not pd.isna(np_w) else float("nan"))
    df["np_watts"] = nps
    df["intensity_factor"] = df["np_watts"] / ftp
    df["tss"] = df.apply(lambda r: tss(r["np_watts"], ftp, r["moving_time"]), axis=1)
    return df


# ----- Weekly / time-series rollups --------------------------------------

def weekly_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Volume by ISO week (week ends Sunday)."""
    d = df.copy()
    d["week"] = d["start_date_local"].dt.to_period("W").dt.start_time
    agg = d.groupby("week").agg(
        rides=("id", "count"),
        hours=("moving_time", lambda s: s.sum() / 3600.0),
        distance_km=("distance", lambda s: s.sum() / 1000.0),
        elevation_m=("total_elevation_gain", "sum"),
        tss=("tss", "sum") if "tss" in d.columns else ("id", "count"),
    )
    if "tss" not in d.columns:
        agg = agg.drop(columns=["tss"])
    return agg


def acute_chronic_ratio(weekly: pd.DataFrame) -> pd.DataFrame:
    """ACR = 7-day TSS / 28-day TSS, computed on a daily rolling basis from weekly rollup.

    Simple approximation using the weekly table: 7d ≈ this week, 28d ≈ last 4 weeks mean × 4.
    """
    if "tss" not in weekly.columns:
        return weekly
    w = weekly.copy()
    w["tss_4wk_avg"] = w["tss"].rolling(4, min_periods=1).mean()
    w["acr"] = w["tss"] / (w["tss_4wk_avg"] * 1.0)  # both are weekly totals
    return w


# ----- HR decoupling ------------------------------------------------------

def hr_decoupling(activity_id: int) -> dict[str, float]:
    """Pa:Hr ratio first half vs second half. >5% decoupling = aerobic stress.

    Returns {"first_half_ratio": x, "second_half_ratio": y, "decoupling_pct": z}.
    """
    streams = load_streams(activity_id)
    w, hr, moving = streams.get("watts"), streams.get("heartrate"), streams.get("moving")
    if w is None or hr is None or w.size < 600:
        return {}
    if moving is not None:
        mask = moving.astype(bool)
        w, hr = w[mask], hr[mask]
        if w.size < 600:
            return {}
    half = w.size // 2
    r1 = float(np.nanmean(w[:half]) / np.nanmean(hr[:half])) if np.nanmean(hr[:half]) else float("nan")
    r2 = float(np.nanmean(w[half:]) / np.nanmean(hr[half:])) if np.nanmean(hr[half:]) else float("nan")
    if np.isnan(r1) or r1 == 0:
        return {"first_half_ratio": r1, "second_half_ratio": r2, "decoupling_pct": float("nan")}
    return {
        "first_half_ratio": r1,
        "second_half_ratio": r2,
        "decoupling_pct": (r1 - r2) / r1 * 100.0,  # positive = power drops relative to HR
    }
