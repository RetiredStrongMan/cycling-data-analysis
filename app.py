"""Local web dashboard for Wilson's AI Coach — multi-user edition.

Run:
    source .venv/bin/activate
    python app.py
Then open http://127.0.0.1:5001 and sign in with Strava.

All data routes require a signed-in user. Each user only sees their own data.
"""
from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from dotenv import load_dotenv
from flask import Flask, abort, g, redirect, render_template, request, url_for

import auth
import storage
import wko

# Load .env so STRAVA_CLIENT_ID/SECRET + SECRET_KEY are available.
ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
# In production (HTTPS), set SESSION_COOKIE_SECURE=1 in the environment.
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE") == "1"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.jinja_env.globals.update(zip=zip, enumerate=enumerate)

auth.init_app(app)

PLOTLY_TEMPLATE = "plotly_white"

# Per-user caches. Keyed by user_id; small max so we don't balloon memory if
# many users are active.
@lru_cache(maxsize=64)
def _pdc_cache(user_id: int) -> pd.DataFrame:
    return wko.power_duration_curve(user_id)


@lru_cache(maxsize=64)
def _pd_model_cache(user_id: int) -> wko.PDModel:
    return wko.fit_pd_model(_pdc_cache(user_id))


def invalidate_caches(user_id: int) -> None:
    """Drop cached PDC / model when a user gets new rides."""
    # lru_cache has no per-key eviction; nuke the whole cache.
    _pdc_cache.cache_clear()
    _pd_model_cache.cache_clear()


def get_rides(user_id: int) -> pd.DataFrame:
    return wko.load_rides(user_id, only_with_power=False)


def fmt_secs(d: int) -> str:
    if d < 60: return f"{d}s"
    if d < 3600: return f"{d // 60}m" + (f"{d % 60}s" if d % 60 else "")
    h, rem = divmod(d, 3600); m = rem // 60
    return f"{h}h" + (f"{m}m" if m else "")


def figure_html(fig: go.Figure, div_id: str | None = None) -> str:
    return pio.to_html(fig, full_html=False, include_plotlyjs="cdn", div_id=div_id,
                       config={"displaylogo": False, "responsive": True})


# =====================================================================
#                          INDEX
# =====================================================================

@app.route("/")
def index():
    """Public landing: redirect to dashboard if signed in, else login."""
    if auth.current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("auth.login"))


# =====================================================================
#                            DATA ROUTES
# =====================================================================

@app.route("/dashboard")
@auth.login_required
def dashboard():
    user = g.user
    pd_m = _pd_model_cache(user.id)
    rides = get_rides(user.id)
    ftp = pd_m.mftp or 200

    rides_with_tss = _rides_with_tss(rides, ftp)
    pmc = wko.performance_management_chart(rides_with_tss, ftp)
    profile = wko.classify_rider(pd_m, weight_kg=user.weight_kg)

    latest = pmc.iloc[-1] if not pmc.empty else None
    ctl = float(latest["ctl"]) if latest is not None else 0.0
    atl = float(latest["atl"]) if latest is not None else 0.0
    tsb = float(latest["tsb"]) if latest is not None else 0.0

    cutoff = pd.Timestamp.now("UTC").tz_localize(None) - pd.Timedelta(days=28)
    rides_28d = rides[rides["start_date"].dt.tz_convert(None) >= cutoff] if not rides.empty else rides

    stats = {
        "total_rides": len(rides),
        "total_km": rides["distance"].sum() / 1000 if not rides.empty else 0,
        "total_hours": rides["moving_time"].sum() / 3600 if not rides.empty else 0,
        "rides_28d": len(rides_28d),
        "hours_28d": rides_28d["moving_time"].sum() / 3600 if not rides_28d.empty else 0,
        "km_28d": rides_28d["distance"].sum() / 1000 if not rides_28d.empty else 0,
        "date_range": (
            (rides["start_date"].min().date(), rides["start_date"].max().date())
            if not rides.empty else (None, None)
        ),
    }

    return render_template(
        "dashboard.html",
        m=pd_m, profile=profile, stats=stats,
        ctl=ctl, atl=atl, tsb=tsb,
        ftp=int(pd_m.mftp), pmax=int(pd_m.pmax),
        frc=pd_m.frc_kj, tte=pd_m.tte_s, tte_label=fmt_secs(pd_m.tte_s),
        stamina=pd_m.stamina,
    )


@app.route("/power-curve")
@auth.login_required
def power_curve():
    user = g.user
    pdc = _pdc_cache(user.id)
    pd_m = _pd_model_cache(user.id)

    valid = pdc.dropna()
    t = valid.index.to_numpy(dtype=float)
    p = valid["watts"].to_numpy(dtype=float)
    t_model = np.logspace(np.log10(60), np.log10(14400), 200)
    p_model = pd_m.cp_raw + pd_m.w_prime / t_model
    y_top = max(float(p.max()) if p.size else 0, 2500.0) * 1.10

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=t, y=p, mode="lines+markers", name="实测最佳功率",
        line=dict(color="#fc4c02", width=2.2), marker=dict(size=6),
        hovertemplate="%{y:.0f} W @ %{customdata}<extra></extra>",
        customdata=[fmt_secs(int(d)) for d in t],
    ))
    fig.add_trace(go.Scatter(
        x=t_model, y=p_model, mode="lines", name="模型 CP 曲线",
        line=dict(color="#1976d2", width=1.4, dash="dash"),
        hovertemplate="模型: %{y:.0f} W<extra></extra>",
    ))
    fig.add_hline(y=pd_m.mftp, line_dash="dot", line_color="#444",
                  annotation_text=f"mFTP {pd_m.mftp:.0f} W", annotation_position="bottom right")
    if pd_m.tte_s:
        fig.add_vline(x=pd_m.tte_s, line_dash="dot", line_color="#888",
                      annotation_text=f"TTE {fmt_secs(pd_m.tte_s)}", annotation_position="top")
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        xaxis_type="log", xaxis_title="时长(对数轴)",
        yaxis_title="功率 (W)", yaxis=dict(range=[0, y_top]),
        xaxis=dict(tickvals=[1, 5, 15, 60, 300, 1200, 3600, 14400],
                   ticktext=["1秒", "5秒", "15秒", "1分", "5分", "20分", "1小时", "4小时"]),
        height=520, margin=dict(l=60, r=30, t=30, b=50),
        legend=dict(orientation="h", y=1.05, x=0),
    )

    key_pts = [(5, "5秒"), (15, "15秒"), (60, "1分钟"), (300, "5分钟"),
               (1200, "20分钟"), (1800, "30分钟"), (3600, "1小时"), (10800, "3小时")]
    needed_ids = {int(pdc.loc[d, "activity_id"]) for d, _ in key_pts
                  if d in pdc.index and pd.notna(pdc.loc[d, "watts"])}
    id_to_name: dict[int, str] = {}
    if needed_ids:
        conn = storage.connect()
        marks = ",".join("?" * len(needed_ids))
        for r in conn.execute(
            f"SELECT id, name FROM activities WHERE user_id = ? AND id IN ({marks})",
            (user.id, *needed_ids),
        ):
            id_to_name[r[0]] = r[1] or f"骑行 {r[0]}"
        conn.close()
    key_table = []
    for d, label in key_pts:
        if d in pdc.index and pd.notna(pdc.loc[d, "watts"]):
            aid = int(pdc.loc[d, "activity_id"])
            key_table.append({
                "label": label,
                "watts": int(pdc.loc[d, "watts"]),
                "pct_ftp": pdc.loc[d, "watts"] / pd_m.mftp * 100 if pd_m.mftp else 0,
                "activity_id": aid,
                "activity_name": id_to_name.get(aid, f"骑行 {aid}"),
            })

    return render_template("power_curve.html",
                           plot=figure_html(fig, "pdc-plot"),
                           m=pd_m, key_table=key_table, fmt_secs=fmt_secs)


@app.route("/rider-profile")
@auth.login_required
def rider_profile():
    user = g.user
    pd_m = _pd_model_cache(user.id)
    profile = wko.classify_rider(pd_m, weight_kg=user.weight_kg)
    pdc = _pdc_cache(user.id)

    sig = [(5, "5s"), (60, "1m"), (300, "5m"), (1200, "20m"), (3600, "1h")]
    sig_vals = []
    for d, label in sig:
        if d in pdc.index and pd.notna(pdc.loc[d, "watts"]):
            sig_vals.append((label, int(pdc.loc[d, "watts"])))

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[s[0] for s in sig_vals], y=[s[1] for s in sig_vals],
        marker_color="#fc4c02", name="你的最佳",
        hovertemplate="%{y} W<extra></extra>",
    ))
    fig.update_layout(template=PLOTLY_TEMPLATE, height=320,
                      yaxis_title="历史最佳功率 (W)",
                      margin=dict(l=50, r=30, t=20, b=40), showlegend=False)

    stamina_fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=pd_m.stamina if math.isfinite(pd_m.stamina) else 0,
        number={"suffix": "%"},
        gauge={
            "axis": {"range": [70, 105]},
            "bar": {"color": "#fc4c02"},
            "steps": [
                {"range": [70, 85], "color": "#ffe0d4"},
                {"range": [85, 95], "color": "#fff3e0"},
                {"range": [95, 105], "color": "#e6f4ea"},
            ],
        },
    ))
    stamina_fig.update_layout(height=260, margin=dict(l=20, r=20, t=20, b=10))

    return render_template("rider_profile.html",
                           profile=profile, m=pd_m,
                           sig_plot=figure_html(fig, "sig-plot"),
                           stamina_plot=figure_html(stamina_fig, "stamina-plot"))


@app.route("/training-load")
@auth.login_required
def training_load():
    user = g.user
    pd_m = _pd_model_cache(user.id)
    ftp = pd_m.mftp or 200
    rides = _rides_with_tss(get_rides(user.id), ftp)
    pmc = wko.performance_management_chart(rides, ftp)

    fig = go.Figure()
    fig.add_trace(go.Bar(x=pmc["date"], y=pmc["tss"], name="每日 TSS",
                         marker_color="#bbb", opacity=0.5, yaxis="y2"))
    fig.add_trace(go.Scatter(x=pmc["date"], y=pmc["ctl"], name="CTL · 体能",
                             line=dict(color="#1976d2", width=2.5)))
    fig.add_trace(go.Scatter(x=pmc["date"], y=pmc["atl"], name="ATL · 疲劳",
                             line=dict(color="#e53935", width=1.8)))
    fig.add_trace(go.Scatter(x=pmc["date"], y=pmc["tsb"], name="TSB · 状态",
                             line=dict(color="#388e3c", width=1.8, dash="dot")))
    fig.update_layout(
        template=PLOTLY_TEMPLATE, height=540,
        yaxis=dict(title="CTL / ATL / TSB (TSS/天)", zeroline=True),
        yaxis2=dict(title="每日 TSS", overlaying="y", side="right", showgrid=False),
        margin=dict(l=60, r=60, t=30, b=50),
        legend=dict(orientation="h", y=1.05, x=0),
        xaxis=dict(rangeslider=dict(visible=True), type="date"),
    )
    latest = pmc.iloc[-1] if not pmc.empty else None
    summary = {
        "ctl": float(latest["ctl"]) if latest is not None else 0,
        "atl": float(latest["atl"]) if latest is not None else 0,
        "tsb": float(latest["tsb"]) if latest is not None else 0,
        "ftp": int(ftp),
    }
    return render_template("training_load.html",
                           plot=figure_html(fig, "pmc-plot"), summary=summary)


@app.route("/zones")
@auth.login_required
def zones():
    user = g.user
    pd_m = _pd_model_cache(user.id)
    ftp = pd_m.mftp or 200

    totals = {name: 0 for name, _, _, _ in wko.COGGAN_ZONES}
    recent = {name: 0 for name, _, _, _ in wko.COGGAN_ZONES}
    cutoff_ts = pd.Timestamp.now("UTC") - pd.Timedelta(days=90)

    rides = get_rides(user.id)[["id", "start_date", "device_watts"]]
    rides = rides[rides["device_watts"] == 1]
    for _, r in rides.iterrows():
        streams = wko.load_streams(user.id, int(r["id"]))
        w = streams.get("watts")
        if w is None:
            continue
        tiz = wko.time_in_zones(w, ftp)
        for k, v in tiz.items():
            totals[k] += v
            if r["start_date"] >= cutoff_ts:
                recent[k] += v

    def to_pct(d):
        s = sum(d.values()) or 1
        return {k: 100.0 * v / s for k, v in d.items()}

    total_pct = to_pct(totals)
    recent_pct = to_pct(recent)
    zones_meta = [{"name": n, "lo": lo, "hi": hi, "color": c} for n, lo, hi, c in wko.COGGAN_ZONES]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[z["name"] for z in zones_meta],
        y=[total_pct[z["name"]] for z in zones_meta],
        marker_color=[z["color"] for z in zones_meta], name="全部时间",
        hovertemplate="%{y:.1f}%<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=[z["name"] for z in zones_meta],
        y=[recent_pct[z["name"]] for z in zones_meta],
        marker_color=[z["color"] for z in zones_meta], marker_pattern_shape="/",
        name="近 90 天",
        hovertemplate="%{y:.1f}%<extra></extra>",
    ))
    fig.update_layout(template=PLOTLY_TEMPLATE, height=460, barmode="group",
                      yaxis_title="占骑行时间 %",
                      margin=dict(l=50, r=30, t=30, b=80))

    return render_template("zones.html",
                           plot=figure_html(fig, "zones-plot"),
                           zones_meta=zones_meta, ftp=int(ftp),
                           total_pct=total_pct, recent_pct=recent_pct,
                           totals=totals, recent=recent)


@app.route("/rides")
@auth.login_required
def rides_list():
    user = g.user
    rides = get_rides(user.id)
    rides = rides[rides["sport_type"].isin(wko.CYCLING_SPORT_TYPES)].copy()
    rides = rides.sort_values("start_date", ascending=False).reset_index(drop=True)
    return render_template("rides.html", rides=rides.to_dict(orient="records"))


@app.route("/ride/<int:activity_id>")
@auth.login_required
def ride_detail(activity_id: int):
    user = g.user
    pd_m = _pd_model_cache(user.id)
    ftp = pd_m.mftp or 200
    streams = wko.load_streams(user.id, activity_id)
    if not streams:
        abort(404)

    conn = storage.connect()
    row = conn.execute(
        "SELECT * FROM activities WHERE user_id = ? AND id = ?",
        (user.id, activity_id),
    ).fetchone()
    conn.close()
    if not row:
        abort(404)
    info = dict(row)

    watts = streams.get("watts", np.array([]))
    hr = streams.get("heartrate")
    time_s = streams.get("time", np.arange(watts.size))
    time_min = time_s / 60.0  # x-axis in minutes for readability

    np_w = wko.normalized_power(watts) if watts.size else float("nan")
    ts = wko.tss(np_w, ftp, info.get("moving_time") or 0)
    tiz = wko.time_in_zones(watts, ftp) if watts.size else {}
    wbal = wko.wbal_skiba(watts, pd_m.cp_raw, pd_m.w_prime) if watts.size and pd_m.cp_raw else np.array([])

    # Lap segmentation: 'smart' (work/rest detection) or 'fixed' (every 1 km)
    lap_mode = request.args.get("mode", "smart")
    if lap_mode == "smart":
        laps = wko.smart_laps(streams, mftp=pd_m.mftp or ftp)
        if not laps:  # fallback if power signal too weak / no mFTP
            laps = wko.compute_laps(streams)
            lap_mode = "fixed"
    else:
        laps = wko.compute_laps(streams)
    selected_lap_idx = request.args.get("lap", type=int)
    selected_lap = None
    if selected_lap_idx and 1 <= selected_lap_idx <= len(laps):
        selected_lap = laps[selected_lap_idx - 1]

    fig = go.Figure()
    if watts.size:
        smooth = pd.Series(watts).rolling(30, min_periods=1).mean()
        fig.add_trace(go.Scatter(x=time_min, y=smooth, name="功率(30 秒平滑)",
                                 line=dict(color="#fc4c02", width=1.2),
                                 hovertemplate="%{x:.1f} 分钟 · %{y:.0f} W<extra></extra>"))
    if wbal.size:
        fig.add_trace(go.Scatter(x=time_min, y=wbal / 1000, name="W′ 余量 (kJ)",
                                 line=dict(color="#1976d2", width=1.4), yaxis="y2",
                                 hovertemplate="%{x:.1f} 分钟 · %{y:.1f} kJ<extra></extra>"))
    if hr is not None and hr.size:
        fig.add_trace(go.Scatter(x=time_min, y=hr, name="心率 (bpm)",
                                 line=dict(color="#7e57c2", width=1.4), yaxis="y3",
                                 hovertemplate="%{x:.1f} 分钟 · %{y:.0f} bpm<extra></extra>"))
    # If a lap is selected, highlight it on the chart and zoom the x-axis to its range.
    if selected_lap is not None:
        fig.add_vrect(
            x0=selected_lap.start_s / 60.0, x1=selected_lap.end_s / 60.0,
            fillcolor="#fc4c02", opacity=0.10, line_width=0,
            annotation_text=f"第 {selected_lap.index} 圈",
            annotation_position="top left",
        )

    fig.update_layout(
        template=PLOTLY_TEMPLATE, height=560,
        yaxis=dict(title="功率 (W)", side="left"),
        yaxis2=dict(title="W' 余量 (kJ)", overlaying="y", side="right"),
        yaxis3=dict(title="心率", overlaying="y", side="right", position=0.97, showgrid=False),
        xaxis_title="时间(分钟)",
        margin=dict(l=60, r=80, t=20, b=50),
        legend=dict(orientation="h", y=1.05, x=0),
    )
    if selected_lap is not None:
        # Add 5% padding on each side so the lap rect is clearly visible
        pad = max(0.5, (selected_lap.end_s - selected_lap.start_s) * 0.05 / 60.0)
        fig.update_xaxes(range=[
            max(0, selected_lap.start_s / 60.0 - pad),
            selected_lap.end_s / 60.0 + pad,
        ])

    zfig = go.Figure()
    zones_meta = [{"name": n, "color": c} for n, _, _, c in wko.COGGAN_ZONES]
    secs = [tiz.get(z["name"], 0) for z in zones_meta]
    zfig.add_trace(go.Bar(
        x=[z["name"] for z in zones_meta], y=secs,
        marker_color=[z["color"] for z in zones_meta],
        hovertemplate="%{x}: %{y:.0f}s<extra></extra>",
    ))
    zfig.update_layout(template=PLOTLY_TEMPLATE, height=300,
                       yaxis_title="秒", margin=dict(l=50, r=30, t=20, b=80))

    wbal_min_kj = float(wbal.min() / 1000) if wbal.size else None
    wbal_min_pct = (wbal_min_kj / pd_m.frc_kj * 100) if (wbal_min_kj is not None and pd_m.frc_kj) else None

    return render_template(
        "ride_detail.html", info=info, np_w=np_w,
        intensity_factor=(np_w / ftp) if (ftp and not math.isnan(np_w)) else None,
        tss=ts, tiz=tiz, fmt_secs=fmt_secs,
        plot=figure_html(fig, "ride-plot"),
        zone_plot=figure_html(zfig, "zone-plot"),
        wbal_min_kj=wbal_min_kj, wbal_min_pct=wbal_min_pct,
        ftp=int(ftp), zones_meta=zones_meta,
        laps=laps, selected_lap=selected_lap, activity_id=activity_id,
        lap_mode=lap_mode,
    )


# ---- Race-strategy preset tables -----------------------------------------
# CdA (frontal-area × drag coeff) defaults by (race_type, gender). Source: range
# of published amateur values; women average ~6% lower due to smaller frontal area.
CDA_BY_RACE_TYPE = {
    "road_race":   {"male": 0.32, "female": 0.30},  # drops, club kit
    "time_trial":  {"male": 0.26, "female": 0.24},  # TT bars, aero skinsuit
    "criterium":   {"male": 0.30, "female": 0.28},  # drops, aggressive
    "hilly":       {"male": 0.34, "female": 0.32},  # hoods on climbs, drops on descents
    "endurance":   {"male": 0.36, "female": 0.34},  # hoods, comfort priority
}

# Map race_type → synthetic-course terrain pattern (used when no GPX uploaded).
TERRAIN_BY_RACE_TYPE = {
    "road_race": "rolling",
    "time_trial": "flat_tt",
    "criterium": "criterium",
    "hilly": "hilly",
    "endurance": "rolling",
}

# Intensity bias by race goal. Multiplies the per-segment %FTP target.
INTENSITY_BY_GOAL = {
    "finish":        0.88,
    "peloton":       0.96,
    "top20":         1.04,
    "podium":        1.10,
    "personal_best": 1.00,
}

GOAL_LABEL = {
    "finish":        "稳妥完赛",
    "peloton":       "跟住主集团",
    "top20":         "冲击前 20%",
    "podium":        "争夺领奖台",
    "personal_best": "个人最佳成绩",
}

GOAL_ADVICE = {
    "finish": "保守配速、平稳输出。把目标定在完赛,不必追逐前组。爬升时按你的 mFTP 配速,平路保持节奏即可。",
    "peloton": "全程贴在主集团边缘——平路省力跟车,关键爬升段做好被拉爆前的心理准备,该咬牙时咬牙。这要求你的功率与集团平均水平相符。",
    "top20": "积极但不盲目。把你的 FRC 储备留给关键攻击或终点冲刺,避免在不必要的早期攻击中烧光火柴。",
    "podium": "战术第一,功率第二。盯紧主要竞争对手,关键时刻不留余地,W′ 全部用完。",
    "personal_best": "按 TTE 区间配速,保持稳定输出贴在 mFTP 附近。这是你能持续做功的最高水平。",
}


def _age_intensity_modifier(age: int) -> float:
    """Older riders can't sustain as much above-FTP work. Apply a small reduction."""
    if age < 40: return 1.00
    if age < 50: return 0.98
    if age < 60: return 0.96
    if age < 70: return 0.93
    return 0.90


@app.route("/race-strategy", methods=["GET", "POST"])
@auth.login_required
def race_strategy():
    import route as route_mod
    user = g.user
    pd_m = _pd_model_cache(user.id)

    # Pull demographics from the user record first; fall back to form values
    # (which include defaults if neither is set).
    distance_km = float(request.values.get("distance_km") or 40)
    weight_kg = float(request.values.get("weight_kg")
                       or (user.weight_kg if user.weight_kg else 70))
    sex_raw = request.values.get("sex")  # 'M' / 'F' from form
    if sex_raw not in ("M", "F"):
        sex_raw = user.sex if user.sex in ("M", "F") else "M"
    gender = "female" if sex_raw == "F" else "male"
    age = int(request.values.get("age")
              or (user.age if user.age else 35))
    race_type = request.values.get("race_type") or "road_race"
    if race_type not in CDA_BY_RACE_TYPE:
        race_type = "road_race"
    goal = request.values.get("goal") or "peloton"
    if goal not in INTENSITY_BY_GOAL:
        goal = "peloton"

    # Persist any user-edited demographics so they stick across visits.
    if request.method == "POST":
        conn = storage.connect()
        try:
            storage.set_user_demographics(
                conn, user.id, sex=sex_raw, age=age, weight_kg=weight_kg,
            )
        finally:
            conn.close()

    # Derive the technical parameters from the user-friendly inputs.
    cda = CDA_BY_RACE_TYPE[race_type][gender]
    terrain = TERRAIN_BY_RACE_TYPE[race_type]
    intensity_bias = INTENSITY_BY_GOAL[goal] * _age_intensity_modifier(age)
    system_mass = weight_kg + 8.0

    course = None
    course_error = None
    plan = None
    climbs: list = []
    profile_plot_html = ""

    if request.method == "POST" and "route_file" in request.files:
        f = request.files["route_file"]
        if f and f.filename:
            try:
                content = f.read()
                if len(content) > 20 * 1024 * 1024:
                    raise ValueError("文件过大(>20 MB)。")
                course = route_mod.parse_gpx(content, name_fallback=f.filename)
                distance_km = course.total_km
            except Exception as e:
                course_error = str(e)

    if course is not None:
        plan = wko.plan_course(course, pd_m,
                               weight_kg=system_mass, cda=cda,
                               intensity_bias=intensity_bias)
        climbs = route_mod.find_climbs(course)
        ele_km = [p.distance_m / 1000.0 for p in course.points]
        ele_m = [p.ele for p in course.points]
        seg_km = [(s.start_km + s.end_km) / 2 for s in plan.segments]
        seg_w = [s.target_power_w for s in plan.segments]
        seg_wbal = [s.wbal_pct for s in plan.segments]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=ele_km, y=ele_m, name="海拔 (m)",
                                 line=dict(color="#888", width=1), fill="tozeroy",
                                 fillcolor="rgba(140,140,140,0.18)",
                                 hovertemplate="%{x:.1f} km · %{y:.0f} m<extra></extra>"))
        fig.add_trace(go.Scatter(x=seg_km, y=seg_w, name="目标功率 (W)",
                                 line=dict(color="#fc4c02", width=2), yaxis="y2",
                                 hovertemplate="%{x:.1f} km · %{y:.0f} W<extra></extra>"))
        fig.add_trace(go.Scatter(x=seg_km, y=seg_wbal, name="W′ 余量 (%)",
                                 line=dict(color="#1976d2", width=1.5, dash="dot"), yaxis="y3",
                                 hovertemplate="%{x:.1f} km · %{y:.0f}%<extra></extra>"))
        fig.update_layout(template=PLOTLY_TEMPLATE, height=460,
                          xaxis_title="距离 (km)",
                          yaxis=dict(title="海拔 (m)", side="left"),
                          yaxis2=dict(title="目标功率 (W)", overlaying="y", side="right"),
                          yaxis3=dict(title="W′ 余量 (%)", overlaying="y", side="right",
                                       position=0.97, range=[0, 105], showgrid=False),
                          legend=dict(orientation="h", y=1.06, x=0),
                          margin=dict(l=60, r=90, t=30, b=50))
        profile_plot_html = figure_html(fig, "course-plot")
    else:
        plan = wko.plan_by_distance(distance_km, pd_m, weight_kg=system_mass,
                                    cda=cda, terrain=terrain)
        kms = [10, 20, 30, 40, 60, 80, 100, 120, 160, 200]
        rows = []
        for km in kms:
            pp = wko.plan_by_distance(km, pd_m, weight_kg=system_mass,
                                       cda=cda, terrain=terrain)
            rows.append({"km": km, "avg_w": pp.avg_power_w})
        df_plot = pd.DataFrame(rows)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df_plot["km"], y=df_plot["avg_w"],
                                 mode="lines+markers",
                                 line=dict(color="#fc4c02", width=2),
                                 name="目标平均功率"))
        fig.add_hline(y=pd_m.mftp, line_dash="dot", line_color="#444",
                      annotation_text=f"mFTP {pd_m.mftp:.0f}W")
        fig.update_layout(template=PLOTLY_TEMPLATE, height=420,
                          xaxis_title="赛事距离 (km)", yaxis_title="目标功率 (W)",
                          margin=dict(l=60, r=30, t=20, b=50))
        profile_plot_html = figure_html(fig, "strategy-plot")

    return render_template(
        "race_strategy.html",
        m=pd_m, plan=plan, course=course, course_error=course_error,
        climbs=climbs,
        distance_km=distance_km, weight_kg=weight_kg,
        gender=gender, age=age, race_type=race_type, goal=goal,
        goal_label=GOAL_LABEL[goal], goal_advice=GOAL_ADVICE[goal],
        user_sex_from_strava=user.sex in ("M", "F"),
        user_weight_from_strava=user.weight_kg is not None,
        plot=profile_plot_html, fmt_secs=fmt_secs,
    )


@app.route("/account/refresh", methods=["POST"])
@auth.login_required
def account_refresh():
    """Trigger an incremental sync + refresh of athlete profile (sex/weight)."""
    import sync as sync_mod
    from strava import StravaClient
    try:
        # Pull the latest /athlete to pick up changes to weight / sex on Strava.
        client = StravaClient.for_user(g.user.id)
        try:
            athlete = client.get("/athlete")
            sex = athlete.get("sex") if athlete.get("sex") in ("M", "F") else None
            weight = athlete.get("weight")
            storage.set_user_demographics(
                client.conn, g.user.id,
                sex=sex,
                weight_kg=weight if weight else None,
            )
        finally:
            client.conn.close()
        sync_mod.run_for_user(g.user.id)
        invalidate_caches(g.user.id)
    except Exception as e:
        app.logger.exception("sync failed for user %s: %s", g.user.id, e)
    return redirect(request.referrer or url_for("dashboard"))


# =====================================================================
#                      HELPERS
# =====================================================================

def _rides_with_tss(rides: pd.DataFrame, ftp: float) -> pd.DataFrame:
    df = rides.copy()
    np_w = df["weighted_average_watts"].fillna(df["average_watts"])
    df["np_watts"] = np_w
    df["tss"] = df.apply(
        lambda r: wko.tss(r["np_watts"], ftp, r["moving_time"])
        if pd.notna(r["np_watts"]) else float("nan"),
        axis=1,
    )
    return df


@app.context_processor
def _inject_globals():
    return {"nav_items": [
        ("总览", "dashboard"),
        ("功率曲线", "power_curve"),
        ("车手画像", "rider_profile"),
        ("训练负荷", "training_load"),
        ("功率区间", "zones"),
        ("骑行记录", "rides_list"),
        ("比赛策略", "race_strategy"),
    ]}


if __name__ == "__main__":
    print("Starting Wilson's AI Coach on http://127.0.0.1:5001")
    app.run(host="127.0.0.1", port=5001, debug=False)
