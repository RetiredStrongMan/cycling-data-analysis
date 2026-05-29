"""WKO5-inspired cycling analytics.

Implements (or approximates, where the WKO5 model is proprietary) the
core power-duration metrics used in modern cycling coaching:

- Pmax           : modeled max instantaneous power
- mFTP           : modeled FTP from the Power-Duration Curve
- FRC            : Functional Reserve Capacity (anaerobic work capacity, kJ)
- TTE            : Time to Exhaustion at mFTP (s)
- Stamina        : fatigue-resistance metric (% of mFTP held for long durations)
- W'bal (DFRC)   : second-by-second tracking of anaerobic reserve during a ride
- Rider type     : Sprinter / Pursuiter / TT / All-Rounder / Climber phenotype
- Power zones    : Coggan classic 7-zone model + iLevels (interval targets)
- Training load  : CTL / ATL / TSB exponentially-weighted averages
- Pacing strategy: target power suggestion for an event duration

Math notes
----------
Power-Duration Curve: we fit the Monod-Scherrer 2-parameter critical-power model
    P(t) = CP + W' / t                          (t in seconds)
over the 3-min to 20-min mean-max points (the sweet spot where CP fits cleanly
and anaerobic capacity is fully expressed). CP ≈ mFTP, W' ≈ FRC*1000.

For very short efforts the Monod model overestimates. We treat Pmax separately
as the all-time peak 1-second power, and FRC is corrected against the actual
best 5-min effort via a small empirical adjustment.

W'bal (Skiba 2012 integral form):
    During P > CP: dW'/dt = -(P - CP)
    During P < CP: dW'/dt = (W'₀ - W') / τ_w
    τ_w (s) = 546 * exp(-0.01 * D_CP) + 316        where D_CP = CP - P_avg(below CP)

Stamina:  ratio of best 60-min power to mFTP. 100% = textbook; lower numbers
          mean fatigue resistance is below typical for this CP.

TTE:      We solve the Monod curve for the time at which mean-max power equals
          mFTP, capped to the observed 20-min to 90-min range.

Training Stress Score:
    IF  = NP / FTP
    TSS = (sec * NP * IF) / (FTP * 3600) * 100

Performance Management Chart (Banister-style EWMAs):
    CTL_t = CTL_{t-1} + (TSS_t - CTL_{t-1}) / 42       (chronic / fitness)
    ATL_t = ATL_{t-1} + (TSS_t - ATL_{t-1}) /  7       (acute / fatigue)
    TSB_t = CTL_t - ATL_t                              (form)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit

import storage

ROOT = Path(__file__).resolve().parent
STREAMS_DIR = ROOT / "data" / "streams"


def _streams_path(user_id: int, activity_id: int) -> Path:
    """Per-user stream path. Falls back to legacy single-user path if the
    new-style file doesn't exist (smooths over partial migrations)."""
    p = STREAMS_DIR / str(user_id) / f"{activity_id}.json"
    if p.exists():
        return p
    legacy = STREAMS_DIR / f"{activity_id}.json"
    return legacy if legacy.exists() else p

# ---- Coggan classic 7-zone model (% of FTP). Upper bound for each zone. ----
COGGAN_ZONES = [
    ("Z1 主动恢复",       0.00, 0.55, "#9e9e9e"),
    ("Z2 有氧耐力",       0.55, 0.75, "#4caf50"),
    ("Z3 节奏",          0.75, 0.90, "#8bc34a"),
    ("Z4 乳酸阈值",       0.90, 1.05, "#ffc107"),
    ("Z5 最大摄氧量",      1.05, 1.20, "#ff9800"),
    ("Z6 无氧能力",       1.20, 1.50, "#f44336"),
    ("Z7 神经肌肉爆发",    1.50, 10.0, "#9c27b0"),
]


# =====================================================================
#                          DATA LOADERS
# =====================================================================

CYCLING_SPORT_TYPES = {
    "Ride", "VirtualRide", "MountainBikeRide", "GravelRide", "EBikeRide",
    "EMountainBikeRide", "Velomobile", "Handcycle",
}


def load_rides(user_id: int, only_cycling: bool = True,
                only_with_power: bool = True) -> pd.DataFrame:
    conn = storage.connect()
    df = pd.read_sql(
        "SELECT * FROM activities WHERE user_id = ?", conn,
        params=(user_id,),
        parse_dates=["start_date", "start_date_local"],
    )
    conn.close()
    if only_cycling:
        df = df[df["sport_type"].isin(CYCLING_SPORT_TYPES)].copy()
    if only_with_power:
        df = df[df["device_watts"] == 1].copy()
    df = df.sort_values("start_date").reset_index(drop=True)
    return df


def load_streams(user_id: int, activity_id: int) -> dict[str, np.ndarray]:
    p = _streams_path(user_id, activity_id)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    out: dict[str, np.ndarray] = {}
    for key, payload in raw.items():
        data = payload.get("data")
        if data is None:
            continue
        out[key] = np.asarray(data)
    return out


def list_power_ride_ids(user_id: int, only_with_streams: bool = True) -> list[int]:
    conn = storage.connect()
    q = "SELECT id FROM activities WHERE user_id = ? AND device_watts = 1"
    if only_with_streams:
        q += " AND streams_fetched_at IS NOT NULL"
    q += " ORDER BY start_date"
    ids = [r[0] for r in conn.execute(q, (user_id,))]
    conn.close()
    return ids


# =====================================================================
#                       POWER-DURATION CURVE
# =====================================================================

DEFAULT_DURATIONS = [
    1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 420, 600, 900,
    1200, 1500, 1800, 2400, 3000, 3600, 4800, 5400, 7200, 10800, 14400,
]


def _best_mean_max(power: np.ndarray, durations: Iterable[int]) -> dict[int, float]:
    out: dict[int, float] = {}
    if power.size == 0:
        return out
    p = np.nan_to_num(power.astype(float), nan=0.0)
    cs = np.concatenate(([0.0], np.cumsum(p)))
    for d in durations:
        if d > p.size:
            out[d] = float("nan")
            continue
        ws = cs[d:] - cs[:-d]
        out[d] = float(ws.max() / d)
    return out


def power_duration_curve(
    user_id: int,
    activity_ids: Iterable[int] | None = None,
    durations: Iterable[int] = DEFAULT_DURATIONS,
) -> pd.DataFrame:
    """Best mean-max power across the user's rides, for each duration."""
    if activity_ids is None:
        activity_ids = list_power_ride_ids(user_id)
    durations = list(durations)
    best = {d: (float("-inf"), None, None) for d in durations}
    for aid in activity_ids:
        streams = load_streams(user_id, aid)
        w = streams.get("watts")
        if w is None or w.size == 0:
            continue
        bmm = _best_mean_max(w, durations)
        for d, val in bmm.items():
            if not math.isfinite(val):
                continue
            if val > best[d][0]:
                best[d] = (val, aid, None)
    rows = [
        {"duration_s": d, "watts": (v if math.isfinite(v) else float("nan")),
         "activity_id": aid}
        for d, (v, aid, _) in best.items()
    ]
    return pd.DataFrame(rows).set_index("duration_s")


# =====================================================================
#                  MODELED POWER-DURATION PARAMETERS
# =====================================================================

@dataclass
class PDModel:
    """Output of the modeled Power-Duration analysis."""
    pmax: float           # W   max 1s power (data peak — Monod doesn't extrapolate this well)
    mftp: float           # W   modeled FTP from CP fit
    frc_kj: float         # kJ  Functional Reserve Capacity (W' / 1000)
    tte_s: int            # s   Time to Exhaustion at mFTP (typically 1800–3600 s)
    stamina: float        # %   60-min best / mFTP (100 = textbook)
    cp_raw: float         # W   raw critical power from Monod fit (slightly > mFTP)
    w_prime: float        # J   anaerobic work capacity (= FRC * 1000)
    fit_r2: float         # R²  goodness of the CP fit (1.0 = perfect)
    fit_points: list[tuple[int, float]]  # (duration_s, watts) actually used in fit

    def as_dict(self) -> dict:
        d = asdict(self)
        d["fit_points"] = list(self.fit_points)
        return d


def _monod(t: np.ndarray, cp: float, w_prime: float) -> np.ndarray:
    return cp + w_prime / t


def fit_pd_model(pdc: pd.DataFrame | None = None,
                  user_id: int | None = None) -> PDModel:
    """Fit the Monod 2-parameter CP model to the Power-Duration Curve.

    Uses the 2-min to 20-min mean-max points (the canonical CP-fit window) and
    derives all WKO-style metrics from the fit + the raw curve.

    Pass either a pre-computed `pdc` DataFrame, or `user_id` to compute it.
    """
    if pdc is None:
        if user_id is None:
            raise ValueError("fit_pd_model needs either pdc or user_id")
        pdc = power_duration_curve(user_id)
    pdc = pdc.dropna()
    if pdc.empty:
        return PDModel(0, 0, 0, 0, 0, 0, 0, 0, [])

    # 1-second peak from the raw curve (Pmax). Monod can't model this.
    pmax = float(pdc.loc[1, "watts"]) if 1 in pdc.index else float(pdc.iloc[0]["watts"])

    # CP fit window: 2 min to 20 min (Monod-Scherrer canonical range).
    mask = (pdc.index >= 120) & (pdc.index <= 1200)
    fit_df = pdc[mask]
    if len(fit_df) < 3:
        # Fallback: use whatever we have between 1 and 20 minutes
        mask = (pdc.index >= 60) & (pdc.index <= 1200)
        fit_df = pdc[mask]
    if len(fit_df) < 2:
        return PDModel(pmax, 0, 0, 0, 0, 0, 0, 0, [])

    t = fit_df.index.to_numpy(dtype=float)
    p = fit_df["watts"].to_numpy(dtype=float)
    try:
        (cp, w_prime), _ = curve_fit(_monod, t, p, p0=[max(p.min() * 0.9, 100), 15000],
                                     bounds=([50, 1000], [600, 60000]), maxfev=5000)
    except Exception:
        # Linear regression on P vs 1/t as a fallback (Monod linearises this way).
        x = 1.0 / t
        slope, intercept = np.polyfit(x, p, 1)
        w_prime, cp = float(slope), float(intercept)
    cp = float(cp)
    w_prime = float(w_prime)

    # R² of the fit
    p_pred = _monod(t, cp, w_prime)
    ss_res = float(np.sum((p - p_pred) ** 2))
    ss_tot = float(np.sum((p - p.mean()) ** 2)) or 1.0
    r2 = 1.0 - ss_res / ss_tot

    # mFTP correction: CP from Monod is ~the highest power sustainable for an
    # asymptotic infinite duration; FTP is conventionally the ~60-min best.
    # WKO5 derives mFTP from a different model, but empirically mFTP ≈ CP * 0.97
    # for trained cyclists with good test efforts in the fit window.
    mftp = cp * 0.97

    # TTE: time at which the fitted curve crosses mFTP. Solve Monod for P=mFTP:
    #   mFTP = cp + w_prime / t  =>  t = w_prime / (mFTP - cp)
    # Since mFTP < cp this is negative; instead use a target slightly below CP:
    # define TTE as the duration where the rider can hold mFTP. Approximate as
    # the longest duration with mean-max >= mFTP from the actual curve, clamped
    # to 1200-5400 s (20-90 min, typical WKO TTE range).
    holding = pdc[pdc["watts"] >= mftp]
    if not holding.empty:
        tte_s = int(min(max(int(holding.index.max()), 1200), 5400))
    else:
        tte_s = 1800

    # 60-min best for stamina ratio
    p60 = float(pdc.loc[3600, "watts"]) if 3600 in pdc.index and pd.notna(pdc.loc[3600, "watts"]) else float("nan")
    stamina = (p60 / mftp * 100.0) if (math.isfinite(p60) and mftp > 0) else float("nan")

    return PDModel(
        pmax=pmax,
        mftp=mftp,
        frc_kj=w_prime / 1000.0,
        tte_s=tte_s,
        stamina=stamina,
        cp_raw=cp,
        w_prime=w_prime,
        fit_r2=r2,
        fit_points=[(int(d), float(w)) for d, w in zip(fit_df.index.tolist(), fit_df["watts"].tolist())],
    )


# =====================================================================
#                         W'BAL (DYNAMIC FRC)
# =====================================================================

def wbal_skiba(watts: np.ndarray, cp: float, w_prime: float) -> np.ndarray:
    """Skiba (2012) integral W'balance model.

    Tracks the anaerobic reserve second by second. When P > CP, W' is depleted
    proportionally; when P < CP, it recovers exponentially with τ_w that depends
    on how far below CP the recovery power is.
    """
    p = np.nan_to_num(watts.astype(float), nan=0.0)
    n = p.size
    if n == 0 or w_prime <= 0:
        return np.array([])

    # Recovery time constant τ_w (Skiba's empirical formula). Faster recovery
    # when P_rec is much lower than CP.
    below = p[p < cp]
    p_below_mean = float(below.mean()) if below.size else float(cp - 100)
    d_cp = max(cp - p_below_mean, 1.0)
    tau_w = 546.0 * math.exp(-0.01 * d_cp) + 316.0

    wbal = np.empty(n, dtype=float)
    w = float(w_prime)
    for i in range(n):
        delta = p[i] - cp
        if delta > 0:
            w -= delta
        else:
            w += (w_prime - w) * (1.0 - math.exp(-1.0 / tau_w))
        if w < 0:
            w = 0.0
        if w > w_prime:
            w = w_prime
        wbal[i] = w
    return wbal


# =====================================================================
#                         NP / IF / TSS / NORMALIZED
# =====================================================================

# =====================================================================
#                            LAPS
# =====================================================================

@dataclass
class Lap:
    """One lap or effort-segment of a ride. Times are seconds from activity start."""
    index: int                 # 1-based lap number
    start_s: float
    end_s: float
    distance_m: float          # length of this lap only
    duration_s: float
    avg_power: float
    np_watts: float            # normalized power for the lap
    max_power: float
    avg_hr: float | None
    max_hr: float | None
    avg_cadence: float | None
    avg_speed_kmh: float
    elev_gain_m: float
    # Smart-segmentation fields (None when using fixed-distance laps)
    kind: str = "lap"          # 'work' | 'rest' | 'lap'
    intensity_label: str | None = None  # 'Recovery' / 'Endurance' / 'Tempo' / 'Threshold' / 'VO2max' / 'Anaerobic' / 'Neuromuscular'


def _lap_from_window(
    index: int, start_idx: int, end_idx: int, streams: dict,
    kind: str = "lap", intensity_label: str | None = None,
) -> Lap | None:
    """Build a Lap from a [start_idx, end_idx] window of streams."""
    distance = streams.get("distance")
    time = streams.get("time")
    watts = streams.get("watts")
    hr = streams.get("heartrate")
    cadence = streams.get("cadence")
    altitude = streams.get("altitude")

    if end_idx <= start_idx:
        return None

    lap_start_s = float(time[start_idx]) if time is not None else float(start_idx)
    lap_end_s   = float(time[end_idx])   if time is not None else float(end_idx)
    lap_dur     = max(lap_end_s - lap_start_s, 1.0)
    lap_dist    = float(distance[end_idx] - distance[start_idx]) if distance is not None else 0.0

    lap_w = watts[start_idx:end_idx + 1] if watts is not None else np.array([])
    if lap_w.size:
        avg_p = float(np.nan_to_num(lap_w).mean())
        max_p = float(np.nanmax(lap_w))
        np_p  = normalized_power(lap_w) if lap_w.size >= 30 else float("nan")
    else:
        avg_p = max_p = np_p = float("nan")

    avg_hr_v = max_hr_v = None
    if hr is not None and hr.size > end_idx:
        lap_hr = hr[start_idx:end_idx + 1]
        if lap_hr.size:
            avg_hr_v = float(np.nan_to_num(lap_hr).mean())
            max_hr_v = float(np.nanmax(lap_hr))

    avg_cad_v = None
    if cadence is not None and cadence.size > end_idx:
        lap_c = cadence[start_idx:end_idx + 1]
        active = lap_c[lap_c > 0]
        if active.size:
            avg_cad_v = float(active.mean())

    elev_gain = 0.0
    if altitude is not None and altitude.size > end_idx:
        lap_alt = altitude[start_idx:end_idx + 1]
        deltas = np.diff(lap_alt)
        elev_gain = float(deltas[deltas > 0].sum())

    return Lap(
        index=index,
        start_s=lap_start_s,
        end_s=lap_end_s,
        distance_m=lap_dist,
        duration_s=lap_dur,
        avg_power=avg_p,
        np_watts=np_p,
        max_power=max_p,
        avg_hr=avg_hr_v,
        max_hr=max_hr_v,
        avg_cadence=avg_cad_v,
        avg_speed_kmh=(lap_dist / lap_dur * 3.6) if lap_dur > 0 else 0.0,
        elev_gain_m=elev_gain,
        kind=kind,
        intensity_label=intensity_label,
    )


def _intensity_label(power_to_ftp: float) -> str:
    """Classify a sustained effort by its fraction of mFTP."""
    if power_to_ftp >= 1.50:  return "神经肌肉"
    if power_to_ftp >= 1.20:  return "无氧"
    if power_to_ftp >= 1.05:  return "VO2max"
    if power_to_ftp >= 0.90:  return "阈值"
    if power_to_ftp >= 0.75:  return "节奏"
    if power_to_ftp >= 0.55:  return "耐力"
    return "恢复"


def smart_laps(
    streams: dict, mftp: float,
    work_threshold: float = 0.90,
    rest_threshold: float = 0.65,
    min_segment_s: float = 30.0,
) -> list[Lap]:
    """Detect work/rest segments using threshold + hysteresis + merging.

    Algorithm:
      1. Smooth watts with a 30-second rolling mean (reduces device noise).
      2. Classify each sample: 'work' (>work_threshold × mFTP),
         'rest' (<rest_threshold × mFTP), or 'transition' (between).
      3. Hysteresis: transition samples inherit the previous non-transition state.
      4. Find contiguous runs of identical state; merge runs shorter than
         `min_segment_s` into the longer adjacent neighbor (preserves structure).
      5. Build Lap objects, labelling each work segment by avg-power intensity.

    Works well for both structured interval workouts (clean alternation between
    intense and easy) and natural rides (long endurance blocks with occasional
    surges).
    """
    watts = streams.get("watts")
    time = streams.get("time")
    if watts is None or watts.size == 0 or mftp <= 0:
        return []

    n = watts.size
    smoothed = pd.Series(watts).rolling(30, min_periods=1).mean().to_numpy()

    work_w = mftp * work_threshold
    rest_w = mftp * rest_threshold
    states = np.zeros(n, dtype=np.int8)   # 0 = transition
    states[smoothed > work_w] = 1
    states[smoothed < rest_w] = -1

    # Hysteresis pass: each transition sample inherits the previous state.
    # Initial leading transitions inherit the first definite state.
    first_def = next((int(s) for s in states if s != 0), 1)
    last = first_def
    for i in range(n):
        if states[i] == 0:
            states[i] = last
        else:
            last = int(states[i])

    # Find contiguous runs
    edges = [0]
    for i in range(1, n):
        if states[i] != states[i - 1]:
            edges.append(i)
    edges.append(n - 1)

    # Build initial segments
    raw: list[dict] = []
    for i in range(len(edges) - 1):
        s_idx, e_idx = edges[i], edges[i + 1]
        s_time = float(time[s_idx]) if time is not None else float(s_idx)
        e_time = float(time[e_idx]) if time is not None else float(e_idx)
        raw.append({
            "start_idx": s_idx, "end_idx": e_idx,
            "state": int(states[s_idx]),
            "duration": e_time - s_time,
        })

    # Iteratively absorb segments shorter than min_segment_s into the longer neighbor
    def merge_short(segs):
        changed = True
        while changed and len(segs) > 1:
            changed = False
            i = 0
            while i < len(segs):
                if segs[i]["duration"] < min_segment_s and len(segs) > 1:
                    # Pick longer neighbor as target
                    if i == 0:
                        tgt = 1
                    elif i == len(segs) - 1:
                        tgt = len(segs) - 2
                    else:
                        tgt = i - 1 if segs[i - 1]["duration"] >= segs[i + 1]["duration"] else i + 1
                    # Merge segs[i] into segs[tgt]
                    if tgt < i:
                        segs[tgt]["end_idx"] = segs[i]["end_idx"]
                    else:
                        segs[tgt]["start_idx"] = segs[i]["start_idx"]
                    s_time = float(time[segs[tgt]["start_idx"]]) if time is not None else float(segs[tgt]["start_idx"])
                    e_time = float(time[segs[tgt]["end_idx"]]) if time is not None else float(segs[tgt]["end_idx"])
                    segs[tgt]["duration"] = e_time - s_time
                    segs.pop(i)
                    changed = True
                    continue
                i += 1
        return segs

    raw = merge_short(raw)

    # Coalesce adjacent same-state segments (can happen after merges)
    coalesced: list[dict] = []
    for seg in raw:
        if coalesced and coalesced[-1]["state"] == seg["state"]:
            coalesced[-1]["end_idx"] = seg["end_idx"]
            coalesced[-1]["duration"] += seg["duration"]
        else:
            coalesced.append(seg)

    laps: list[Lap] = []
    for i, seg in enumerate(coalesced, start=1):
        kind = "work" if seg["state"] == 1 else "rest"
        lap = _lap_from_window(i, seg["start_idx"], seg["end_idx"], streams)
        if lap is None:
            continue
        lap.kind = kind
        if kind == "work" and mftp > 0 and not math.isnan(lap.avg_power):
            lap.intensity_label = _intensity_label(lap.avg_power / mftp)
        else:
            lap.intensity_label = "恢复"
        laps.append(lap)
    # Re-number contiguously after merges
    for new_idx, lap in enumerate(laps, start=1):
        lap.index = new_idx
    return laps


def compute_laps(streams: dict, lap_distance_m: float = 1000.0) -> list[Lap]:
    """Split an activity into fixed-distance laps using the distance stream.

    Strava's UI default is 1 km auto-laps; we match that. The first and last
    laps may be shorter if the route doesn't divide evenly.
    """
    distance = streams.get("distance")
    if distance is None or distance.size == 0:
        return []
    time = streams.get("time")
    watts = streams.get("watts")
    hr = streams.get("heartrate")
    cadence = streams.get("cadence")
    altitude = streams.get("altitude")

    total_m = float(distance[-1])
    if total_m < lap_distance_m * 0.5:
        # Activity shorter than half a lap — return the whole thing as lap 1
        boundaries = [0, distance.size - 1]
    else:
        n_laps = max(1, int(np.ceil(total_m / lap_distance_m)))
        boundaries = [0]
        for i in range(1, n_laps + 1):
            target_m = i * lap_distance_m
            if target_m >= total_m:
                idx = distance.size - 1
            else:
                idx = int(np.searchsorted(distance, target_m, side="left"))
            boundaries.append(min(idx, distance.size - 1))

    laps: list[Lap] = []
    for i in range(len(boundaries) - 1):
        start_idx, end_idx = boundaries[i], boundaries[i + 1]
        if end_idx <= start_idx:
            continue

        lap_start_s = float(time[start_idx]) if time is not None else float(start_idx)
        lap_end_s   = float(time[end_idx])   if time is not None else float(end_idx)
        lap_dur     = max(lap_end_s - lap_start_s, 1.0)
        lap_dist    = float(distance[end_idx] - distance[start_idx])

        lap_w = watts[start_idx:end_idx + 1] if watts is not None else np.array([])
        if lap_w.size:
            avg_p = float(np.nan_to_num(lap_w).mean())
            max_p = float(np.nanmax(lap_w))
            np_p  = normalized_power(lap_w) if lap_w.size >= 30 else float("nan")
        else:
            avg_p = max_p = np_p = float("nan")

        avg_hr_v = max_hr_v = None
        if hr is not None and hr.size > end_idx:
            lap_hr = hr[start_idx:end_idx + 1]
            if lap_hr.size:
                avg_hr_v = float(np.nan_to_num(lap_hr).mean())
                max_hr_v = float(np.nanmax(lap_hr))

        avg_cad_v = None
        if cadence is not None and cadence.size > end_idx:
            lap_c = cadence[start_idx:end_idx + 1]
            active = lap_c[lap_c > 0]  # ignore coasting
            if active.size:
                avg_cad_v = float(active.mean())

        elev_gain = 0.0
        if altitude is not None and altitude.size > end_idx:
            lap_alt = altitude[start_idx:end_idx + 1]
            deltas = np.diff(lap_alt)
            elev_gain = float(deltas[deltas > 0].sum())

        laps.append(Lap(
            index=i + 1,
            start_s=lap_start_s,
            end_s=lap_end_s,
            distance_m=lap_dist,
            duration_s=lap_dur,
            avg_power=avg_p,
            np_watts=np_p,
            max_power=max_p,
            avg_hr=avg_hr_v,
            max_hr=max_hr_v,
            avg_cadence=avg_cad_v,
            avg_speed_kmh=(lap_dist / lap_dur * 3.6) if lap_dur > 0 else 0.0,
            elev_gain_m=elev_gain,
        ))
    return laps


def normalized_power(watts: np.ndarray) -> float:
    p = np.nan_to_num(watts.astype(float), nan=0.0)
    if p.size < 30:
        return float("nan")
    cs = np.concatenate(([0.0], np.cumsum(p)))
    roll = (cs[30:] - cs[:-30]) / 30.0
    return float(np.power(np.mean(np.power(roll, 4)), 0.25))


def tss(np_watts: float, ftp: float, moving_time_s: float) -> float:
    if not (np_watts and ftp and moving_time_s) or math.isnan(np_watts) or ftp == 0:
        return float("nan")
    intensity = np_watts / ftp
    return (moving_time_s * np_watts * intensity) / (ftp * 3600.0) * 100.0


def ride_np(row) -> float:
    """Canonical Normalized Power for a single ride — the SINGLE source of truth.

    Every module (ride detail, dashboard, training-load PMC) must derive a
    ride's NP through this function so the same ride never shows a different
    NP / IF / TSS on different pages.

    Priority:
      1. Strava's stored NP (`weighted_average_watts`) — computed by Strava with
         the same 30s-rolling / 4th-power algorithm we'd use, available on the
         activity summary, and cheap (no stream load).
      2. `average_watts` — last resort when Strava didn't report NP (e.g. very
         short or estimated-power rides).

    Deliberately does NOT recompute from streams: doing that on the ride-detail
    page but using the stored value in the rollup is exactly the inconsistency
    we're eliminating. `row` may be a dict or a pandas Series — both support .get().
    """
    np_w = row.get("weighted_average_watts")
    if np_w is not None and not pd.isna(np_w) and np_w > 0:
        return float(np_w)
    avg = row.get("average_watts")
    if avg is not None and not pd.isna(avg) and avg > 0:
        return float(avg)
    return float("nan")


def ride_tss(row, ftp: float) -> float:
    """Canonical per-ride TSS, built on ride_np() so it matches everywhere."""
    return tss(ride_np(row), ftp, row.get("moving_time") or 0)


def time_in_zones(watts: np.ndarray, ftp: float) -> dict[str, int]:
    """Return seconds spent in each Coggan zone. Caller can divide by total for %."""
    out = {name: 0 for name, _, _, _ in COGGAN_ZONES}
    if watts.size == 0 or ftp <= 0:
        return out
    p = np.nan_to_num(watts.astype(float), nan=0.0)
    ratios = p / ftp
    for name, lo, hi, _ in COGGAN_ZONES:
        out[name] = int(np.sum((ratios >= lo) & (ratios < hi)))
    return out


# =====================================================================
#                  RIDER TYPE / PHENOTYPE
# =====================================================================

@dataclass
class RiderProfile:
    rider_type: str            # Sprinter / Pursuiter / TT / All-Rounder / Climber
    confidence: float          # 0-1
    pmax_ftp_ratio: float
    frc_ftp_ratio: float       # kJ per W of FTP (typical 0.05–0.12)
    stamina_score: float       # 0-100 — higher = more endurance-oriented
    notes: list[str]


def classify_rider(pd_model: PDModel, weight_kg: float | None = None) -> RiderProfile:
    """Classify rider phenotype from PD model parameters.

    Heuristic (mirrors WKO5's qualitative phenotype categories — exact thresholds
    are proprietary but the ratios used here align with published norms):

      - High Pmax/FTP (>5.0)  AND high FRC/FTP (>0.10)  → Sprinter
      - High FRC/FTP (>0.10)  AND moderate Pmax/FTP     → Pursuiter
      - Low Pmax/FTP (<3.5)   AND low FRC/FTP (<0.06)   → Time Trialist
      - High FTP (W/kg > 4.5) AND high stamina (>92%)   → Climber (if weight known)
      - Balanced                                         → All-Rounder
    """
    if pd_model.mftp <= 0:
        return RiderProfile("未知", 0.0, 0, 0, 0, ["功率数据不足。"])
    pmax_r = pd_model.pmax / pd_model.mftp
    frc_r = pd_model.frc_kj / pd_model.mftp
    stamina = pd_model.stamina if math.isfinite(pd_model.stamina) else 90.0
    wkg = (pd_model.mftp / weight_kg) if weight_kg else None

    candidates: list[tuple[str, float, str]] = []
    if pmax_r >= 5.0 and frc_r >= 0.10:
        candidates.append(("冲刺型", min(1.0, (pmax_r - 4.5) / 2.0),
                          f"Pmax/FTP={pmax_r:.1f}、FRC/FTP={frc_r:.2f},冲刺与无氧储备同时偏高。"))
    if frc_r >= 0.10 and 4.0 <= pmax_r <= 6.0:
        candidates.append(("追逐型", min(1.0, frc_r / 0.15),
                          f"无氧能力突出(FRC/FTP={frc_r:.2f}),冲刺中等。"))
    if pmax_r <= 3.5 and frc_r <= 0.06:
        candidates.append(("计时赛型", min(1.0, (4.0 - pmax_r) / 2.0),
                          f"有氧主导:冲刺低(Pmax/FTP={pmax_r:.1f}),FRC 偏低。"))
    if wkg and wkg >= 4.3 and stamina >= 90:
        candidates.append(("爬坡型", min(1.0, (wkg - 4.0) / 2.0),
                          f"FTP={wkg:.1f} W/kg,耐力强({stamina:.0f}%)。"))
    if not candidates:
        candidates.append(("全能型", 0.6,
                          f"各项均衡:Pmax/FTP={pmax_r:.1f}、FRC/FTP={frc_r:.2f}、耐力={stamina:.0f}%。"))

    best = max(candidates, key=lambda c: c[1])
    notes = [c[2] for c in candidates]
    stamina_score = max(0.0, min(100.0, stamina))
    return RiderProfile(
        rider_type=best[0],
        confidence=best[1],
        pmax_ftp_ratio=pmax_r,
        frc_ftp_ratio=frc_r,
        stamina_score=stamina_score,
        notes=notes,
    )


# =====================================================================
#                     TRAINING LOAD (PMC)
# =====================================================================

def performance_management_chart(
    rides: pd.DataFrame, ftp: float
) -> pd.DataFrame:
    """Daily CTL/ATL/TSB Banister-style EWMAs from a rides DataFrame.

    Requires `start_date` (datetime). Uses the `tss` column if present; otherwise
    derives it via the canonical ride_tss() so it can never disagree with the
    per-ride TSS shown elsewhere.
    """
    if rides.empty:
        return pd.DataFrame(columns=["date", "tss", "ctl", "atl", "tsb"])

    d = rides.copy()
    if "tss" not in d.columns or d["tss"].isna().all():
        d["tss"] = d.apply(lambda r: ride_tss(r, ftp), axis=1)

    d["date"] = d["start_date"].dt.tz_convert(None).dt.normalize()
    daily = d.groupby("date", as_index=False)["tss"].sum()
    full_range = pd.date_range(start=daily["date"].min(), end=daily["date"].max(), freq="D")
    daily = daily.set_index("date").reindex(full_range, fill_value=0).reset_index()
    daily.columns = ["date", "tss"]

    ctl = np.zeros(len(daily))
    atl = np.zeros(len(daily))
    for i, t in enumerate(daily["tss"].to_numpy()):
        prev_ctl = ctl[i - 1] if i else 0.0
        prev_atl = atl[i - 1] if i else 0.0
        ctl[i] = prev_ctl + (t - prev_ctl) / 42.0
        atl[i] = prev_atl + (t - prev_atl) /  7.0
    daily["ctl"] = ctl
    daily["atl"] = atl
    daily["tsb"] = ctl - atl
    return daily


# =====================================================================
#                       RACE STRATEGY / PACING
# =====================================================================

@dataclass
class PacingRecommendation:
    target_power_w: int
    target_pct_ftp: float
    expected_tss: float
    notes: str


# =====================================================================
#               POWER ⇄ SPEED (bicycle physics)
# =====================================================================

def power_to_speed(
    power_w: float, grade: float,
    weight_kg: float = 83.0, cda: float = 0.32, crr: float = 0.005,
    rho: float = 1.225, eta: float = 0.97,
) -> float:
    """Steady-state velocity (m/s) for a given input power on a given grade.

    Solves the standard cycling power equation
        P · η = m·g·v·(grade + Crr) + 0.5·ρ·CdA·v³
    for v.  Negative grades (descents) reduce the rolling/gravity term and
    may even allow positive velocity at zero or negative input power
    (coasting / braking).

    Defaults assume rider+bike 83 kg, drop-bar position, smooth asphalt,
    sea-level air. Override `weight_kg`/`cda` for individualization.
    """
    g = 9.81
    a = 0.5 * rho * cda
    b = weight_kg * g * (grade + crr)
    c = power_w * eta

    def f(v): return a * v * v * v + b * v - c

    lo, hi = 0.5, 30.0  # 1.8 km/h to 108 km/h
    f_lo, f_hi = f(lo), f(hi)
    # If even at hi we still need more power (rare), expand.
    while f_hi < 0 and hi < 50:
        hi *= 1.5
        f_hi = f(hi)
    # If at lo we're already overshooting (steep descent + low power), descent
    # speed is gravity-limited — return hi as upper bound.
    if f_lo > 0 and f_hi > 0:
        # cubic monotonically increasing; root is below lo. Return lo as floor.
        return lo
    if f_lo < 0 and f_hi < 0:
        return hi
    # Bracketed: bisection (15 iterations is enough for 0.001 m/s precision)
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        fm = f(mid)
        if fm == 0:
            return mid
        if (fm < 0) == (f_lo < 0):
            lo, f_lo = mid, fm
        else:
            hi, f_hi = mid, fm
    return 0.5 * (lo + hi)


# =====================================================================
#               COURSE-AWARE PACING PLAN
# =====================================================================

@dataclass
class SegmentPlan:
    start_km: float
    end_km: float
    distance_m: float
    grade_pct: float
    target_power_w: int
    pct_ftp: float
    speed_kmh: float
    duration_s: float
    cum_time_s: float
    wbal_kj: float          # remaining W' at end of segment
    wbal_pct: float         # remaining as % of FRC


@dataclass
class CoursePlan:
    segments: list[SegmentPlan]
    total_distance_km: float
    total_elev_gain_m: float
    total_time_s: float
    avg_speed_kmh: float
    avg_power_w: int
    expected_tss: float
    notes: str
    wbal_min_kj: float       # lowest W' point on the course
    wbal_min_pct: float


def _grade_to_target_pct_ftp(grade_pct: float, intensity_multiplier: float = 1.0) -> float:
    """Variable Power Principle: ride harder where it counts (climbs), easier
    where it doesn't (descents). Returns a target as a fraction of mFTP.

    `intensity_multiplier` scales the whole curve up/down — the caller picks
    this based on overall event duration vs the rider's TTE.
    """
    # Anchor points: (grade%, %FTP_at_grade_0). Linear interpolation between.
    if grade_pct >= 8:    base = 1.18
    elif grade_pct >= 5:  base = 1.10
    elif grade_pct >= 3:  base = 1.02
    elif grade_pct >= 1:  base = 0.95
    elif grade_pct >= -1: base = 0.90  # flat
    elif grade_pct >= -3: base = 0.70  # gentle down
    elif grade_pct >= -6: base = 0.40  # moderate down — start coasting
    else:                 base = 0.20  # steep down — brake-feathering
    return base * intensity_multiplier


def _intensity_for_distance(distance_km: float, mftp: float, tte_s: int) -> float:
    """Scale factor on the grade curve based on how the event compares to TTE.

    Short event → higher overall intensity (you can dip into FRC).
    Long event  → lower intensity (durability matters more).
    """
    # Heuristic: estimate baseline event time at 90% of mFTP avg speed flat
    base_speed = power_to_speed(mftp * 0.90, 0)
    est_s = (distance_km * 1000.0) / max(base_speed, 1.0)
    ratio = est_s / max(tte_s, 1)
    if ratio <= 0.5:   return 1.05
    if ratio <= 1.0:   return 1.00
    if ratio <= 2.0:   return 0.96
    if ratio <= 4.0:   return 0.92
    return 0.85


def plan_course(
    course,                                                 # route.Course
    pd_model: PDModel,
    weight_kg: float = 83.0,
    cda: float = 0.32,
    crr: float = 0.005,
    intensity_bias: float = 1.0,                            # user multiplier
) -> CoursePlan:
    """Build a per-segment pacing plan for a real course, tracking W'bal."""
    from route import segment_course
    segments_raw = segment_course(course, segment_m=500.0)

    mftp = max(pd_model.mftp, 1.0)
    frc_j = pd_model.frc_kj * 1000.0
    cp = pd_model.cp_raw or mftp
    tau_w = 546.0 * math.exp(-0.01 * max(cp - mftp * 0.6, 1.0)) + 316.0

    intensity_mult = _intensity_for_distance(course.total_km, mftp, pd_model.tte_s) * intensity_bias

    plans: list[SegmentPlan] = []
    cum_time = 0.0
    wbal = frc_j
    wbal_min = wbal

    sum_power_time = 0.0
    sum_time = 0.0

    for seg in segments_raw:
        pct = _grade_to_target_pct_ftp(seg.grade_pct, intensity_mult)
        target_p = mftp * pct
        v = power_to_speed(target_p, seg.grade_pct / 100.0,
                           weight_kg=weight_kg, cda=cda, crr=crr)
        v = max(v, 1.0)
        dt = seg.distance_m / v

        # Update W'bal across this segment (constant-power approximation)
        if target_p > cp:
            wbal -= (target_p - cp) * dt
        else:
            # exponential refill toward FRC with τ_w
            wbal = frc_j - (frc_j - wbal) * math.exp(-dt / tau_w)
        wbal = max(0.0, min(frc_j, wbal))
        if wbal < wbal_min:
            wbal_min = wbal

        cum_time += dt
        sum_power_time += target_p * dt
        sum_time += dt

        plans.append(SegmentPlan(
            start_km=seg.start_km,
            end_km=seg.end_km,
            distance_m=seg.distance_m,
            grade_pct=seg.grade_pct,
            target_power_w=int(round(target_p)),
            pct_ftp=pct * 100,
            speed_kmh=v * 3.6,
            duration_s=dt,
            cum_time_s=cum_time,
            wbal_kj=wbal / 1000.0,
            wbal_pct=(wbal / frc_j * 100.0) if frc_j else 0.0,
        ))

    avg_power = (sum_power_time / sum_time) if sum_time else 0.0
    avg_speed = (course.total_distance_m / cum_time * 3.6) if cum_time else 0.0
    exp_tss = tss(avg_power, mftp, cum_time)

    # Coach's notes
    note_parts = []
    if course.total_elev_gain >= 1500:
        note_parts.append("总爬升较大,以爬段配速为先,下坡积极恢复 W′ 余量。")
    elif course.total_elev_gain >= 500:
        note_parts.append("起伏地形,在爬升段保持节奏,平路顺势推 90% mFTP。")
    else:
        note_parts.append("近似平路,保持稳定输出,避免功率波动超过 ±5%。")
    if wbal_min / frc_j < 0.2:
        note_parts.append(f"沿途 W′ 余量最低降至 {wbal_min/1000:.1f} kJ"
                         f"({wbal_min/frc_j*100:.0f}% 的 FRC),爬坡时要节制爆发。")
    if avg_speed > 35:
        note_parts.append("预计平均速度偏高,核对 CdA / 重量参数是否符合实情。")

    return CoursePlan(
        segments=plans,
        total_distance_km=course.total_km,
        total_elev_gain_m=course.total_elev_gain,
        total_time_s=cum_time,
        avg_speed_kmh=avg_speed,
        avg_power_w=int(round(avg_power)),
        expected_tss=exp_tss,
        notes=" ".join(note_parts),
        wbal_min_kj=wbal_min / 1000.0,
        wbal_min_pct=(wbal_min / frc_j * 100.0) if frc_j else 0.0,
    )


def plan_by_distance(
    distance_km: float, pd_model: PDModel,
    weight_kg: float = 83.0, cda: float = 0.32, crr: float = 0.005,
    terrain: str = "rolling",
) -> CoursePlan:
    """No GPX uploaded — generate a synthetic course of the given distance
    with terrain-typical grade distribution, then run the same planner.

    Useful for "I'm going to ride 40 km of rolling road, what should I do?"
    """
    # Build a synthetic course as a list of (distance_m, ele) points.
    from route import Course, RoutePoint
    n_segments = max(8, int(distance_km * 2))  # 500m resolution
    seg_len = (distance_km * 1000.0) / n_segments
    # Grade pattern per terrain.
    if terrain == "flat_tt":
        pattern = [0.0] * n_segments
    elif terrain == "rolling":
        # Sinusoidal ±2% grade
        pattern = [0.02 * math.sin(2 * math.pi * i / 8) for i in range(n_segments)]
    elif terrain == "hilly":
        # Two big climbs at 1/3 and 2/3 of the course
        pattern = []
        for i in range(n_segments):
            x = i / n_segments
            if 0.30 < x < 0.42 or 0.62 < x < 0.74:
                pattern.append(0.06)
            elif 0.42 < x < 0.55 or 0.74 < x < 0.85:
                pattern.append(-0.05)
            else:
                pattern.append(0.005 * math.sin(20 * math.pi * x))
    elif terrain == "criterium":
        # Mostly flat with short 4% pinches every ~1km
        pattern = []
        for i in range(n_segments):
            pattern.append(0.04 if i % 4 == 0 else 0.0)
    else:
        pattern = [0.0] * n_segments

    cum_dist = 0.0
    ele = 100.0  # arbitrary start
    points: list[RoutePoint] = [RoutePoint(lat=0, lon=0, ele=ele, distance_m=0, grade=pattern[0])]
    for i, grade in enumerate(pattern):
        d_seg = seg_len
        ele += grade * d_seg
        cum_dist += d_seg
        points.append(RoutePoint(lat=0, lon=0, ele=ele, distance_m=cum_dist, grade=grade))

    course = Course(name=f"{distance_km:.0f} km · {terrain}", points=points,
                    total_distance_m=cum_dist,
                    total_elev_gain=sum(g * seg_len for g in pattern if g > 0),
                    total_elev_loss=sum(-g * seg_len for g in pattern if g < 0),
                    max_ele=max(p.ele for p in points),
                    min_ele=min(p.ele for p in points))
    return plan_course(course, pd_model, weight_kg=weight_kg, cda=cda, crr=crr)


def pace_for_event(
    pd_model: PDModel, event_duration_min: int, terrain: str = "rolling",
) -> PacingRecommendation:
    """Recommend a target power for a given event duration.

    Strategy:
      - For events <= TTE_min: aim for mFTP (you're in your sustainable threshold).
      - For events <= 0.5 * TTE_min: push toward 1.05-1.10 × mFTP (above-threshold).
      - For events > TTE_min: drop below mFTP based on the modeled curve.
        Use the actual best mean-max power for that duration if it exists,
        otherwise extrapolate from the curve.
    """
    t = event_duration_min * 60
    mftp = pd_model.mftp
    tte = pd_model.tte_s

    if t <= max(60, tte // 4):  # very short
        target_p = mftp * 1.08
        note = f"短距离强度赛(不到 {tte//240} 分钟)——可以在 mFTP 之上推 8%。"
    elif t <= tte:
        target_p = mftp
        note = f"在你的 TTE 范围内({tte//60} 分钟)——全程稳定保持 mFTP。"
    elif t <= tte * 2:
        target_p = mftp * 0.94
        note = f"超过 TTE——回落约 6% 以延长时长,避免耗尽 FRC。"
    elif t <= tte * 4:
        target_p = mftp * 0.88
        note = f"长距离赛事——耐力配速,明显低于 TTE。"
    else:
        target_p = mftp * 0.78
        note = f"超长距离——补给与持久力比功率更关键。"

    if terrain == "hilly":
        note += " 把火柴留给爬坡:爬坡 110–120%,下坡 60–70%。"
        target_p *= 1.0  # average stays the same; variable below
    elif terrain == "flat_tt":
        note += " 保持稳定,空气动力学主导,避免超目标 5% 以上的冲刺。"
    elif terrain == "criterium":
        note += " 比赛由冲刺节奏决定——盯住 W′ 余量,而不是平均功率。"

    exp_tss = tss(target_p, mftp, t)
    return PacingRecommendation(
        target_power_w=int(round(target_p)),
        target_pct_ftp=target_p / mftp * 100 if mftp else 0,
        expected_tss=exp_tss,
        notes=note,
    )
