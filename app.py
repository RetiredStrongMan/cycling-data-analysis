"""Local web dashboard for Wilson's AI Coach.

Run:
    source .venv/bin/activate
    python app.py
Then open http://127.0.0.1:5001 in your browser.

Sections:
  /                  Dashboard — key WKO-style metrics at a glance
  /power-curve       Modeled Power-Duration Curve + mFTP / FRC / TTE fit
  /rider-profile     Phenotype classification + sprint-vs-endurance balance
  /training-load     CTL / ATL / TSB (Performance Management Chart)
  /zones             Lifetime + recent time-in-zone distribution
  /rides             Browse all rides
  /ride/<id>         Single-ride analysis: NP, IF, TSS, zones, W'bal trace
  /race-strategy     Pacing recommendation for a target event duration
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from flask import Flask, abort, render_template, request

import wko

app = Flask(__name__)
app.jinja_env.globals.update(zip=zip, enumerate=enumerate)

PLOTLY_TEMPLATE = "plotly_white"


# =====================================================================
#                  CACHED HEAVY COMPUTATIONS
# =====================================================================

@lru_cache(maxsize=1)
def _pdc_cache() -> pd.DataFrame:
    return wko.power_duration_curve()


@lru_cache(maxsize=1)
def _pd_model_cache() -> wko.PDModel:
    return wko.fit_pd_model(_pdc_cache())


@lru_cache(maxsize=1)
def _rides_cache_key() -> int:
    """Invalidation key — count of rides + max start_date. Cheap to compute."""
    df = wko.load_rides(only_with_power=False)
    return len(df)


def get_rides() -> pd.DataFrame:
    return wko.load_rides(only_with_power=False)


def fmt_secs(d: int) -> str:
    if d < 60: return f"{d}s"
    if d < 3600: return f"{d // 60}m" + (f"{d % 60}s" if d % 60 else "")
    h, rem = divmod(d, 3600); m = rem // 60
    return f"{h}h" + (f"{m}m" if m else "")


def figure_html(fig: go.Figure, div_id: str | None = None) -> str:
    return pio.to_html(fig, full_html=False, include_plotlyjs="cdn", div_id=div_id,
                       config={"displaylogo": False, "responsive": True})


# =====================================================================
#                            ROUTES
# =====================================================================

@app.route("/")
def dashboard():
    pd_m = _pd_model_cache()
    rides = get_rides()
    ftp = pd_m.mftp or 200

    # Compute / cache TSS per ride for the PMC
    rides_with_tss = _rides_with_tss(rides, ftp)
    pmc = wko.performance_management_chart(rides_with_tss, ftp)
    profile = wko.classify_rider(pd_m)

    # Today's PMC values
    latest = pmc.iloc[-1] if not pmc.empty else None
    ctl = float(latest["ctl"]) if latest is not None else 0.0
    atl = float(latest["atl"]) if latest is not None else 0.0
    tsb = float(latest["tsb"]) if latest is not None else 0.0

    # Recent stats (last 28 days)
    cutoff = pd.Timestamp.now("UTC").tz_localize(None) - pd.Timedelta(days=28)
    rides_28d = rides[rides["start_date"].dt.tz_convert(None) >= cutoff]

    stats = {
        "total_rides": len(rides),
        "total_km": rides["distance"].sum() / 1000,
        "total_hours": rides["moving_time"].sum() / 3600,
        "rides_28d": len(rides_28d),
        "hours_28d": rides_28d["moving_time"].sum() / 3600,
        "km_28d": rides_28d["distance"].sum() / 1000,
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
def power_curve():
    pdc = _pdc_cache()
    pd_m = _pd_model_cache()

    valid = pdc.dropna()
    t = valid.index.to_numpy(dtype=float)
    p = valid["watts"].to_numpy(dtype=float)

    # Modeled fit line: only plot the Monod curve in its valid range. Below ~60s
    # the model diverges to infinity (P = CP + W'/t), which is unphysical and
    # blows up the y-axis. Restrict to 60s and longer.
    t_model = np.logspace(np.log10(60), np.log10(14400), 200)
    p_model = pd_m.cp_raw + pd_m.w_prime / t_model

    # Y-axis cap based on elite sprinter peak (~2500 W). Use the larger of the
    # observed Pmax and 2500 W with a 10% headroom — so the chart frames the
    # rider's data tightly without clipping their actual peak.
    y_top = max(float(p.max()), 2500.0) * 1.10

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=t, y=p, mode="lines+markers", name="实测最佳功率",
        line=dict(color="#fc4c02", width=2.2),
        marker=dict(size=6),
        hovertemplate="%{y:.0f} W @ %{customdata}<extra></extra>",
        customdata=[fmt_secs(int(d)) for d in t],
    ))
    fig.add_trace(go.Scatter(
        x=t_model, y=p_model, mode="lines", name="模型 CP 曲线",
        line=dict(color="#1976d2", width=1.4, dash="dash"),
        hovertemplate="模型: %{y:.0f} W<extra></extra>",
    ))
    # mFTP horizontal
    fig.add_hline(y=pd_m.mftp, line_dash="dot", line_color="#444",
                  annotation_text=f"mFTP {pd_m.mftp:.0f} W", annotation_position="bottom right")
    # TTE marker
    if pd_m.tte_s:
        fig.add_vline(x=pd_m.tte_s, line_dash="dot", line_color="#888",
                      annotation_text=f"TTE {fmt_secs(pd_m.tte_s)}", annotation_position="top")

    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        xaxis_type="log", xaxis_title="时长(对数轴)",
        yaxis_title="功率 (W)",
        xaxis=dict(tickvals=[1, 5, 15, 60, 300, 1200, 3600, 14400],
                   ticktext=["1秒", "5秒", "15秒", "1分", "5分", "20分", "1小时", "4小时"]),
        yaxis=dict(range=[0, y_top]),
        height=520, margin=dict(l=60, r=30, t=30, b=50),
        legend=dict(orientation="h", y=1.05, x=0),
    )

    # Key duration table — look up the ride NAME (not just id) for each best effort.
    key_pts = [(5, "5秒"), (15, "15秒"), (60, "1分钟"), (300, "5分钟"),
               (1200, "20分钟"), (1800, "30分钟"), (3600, "1小时"), (10800, "3小时")]
    needed_ids = {int(pdc.loc[d, "activity_id"]) for d, _ in key_pts
                  if d in pdc.index and pd.notna(pdc.loc[d, "watts"])}
    id_to_name: dict[int, str] = {}
    if needed_ids:
        conn = __import__("storage").connect()
        marks = ",".join("?" * len(needed_ids))
        for r in conn.execute(f"SELECT id, name FROM activities WHERE id IN ({marks})", tuple(needed_ids)):
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
def rider_profile():
    pd_m = _pd_model_cache()
    profile = wko.classify_rider(pd_m)
    pdc = _pdc_cache()

    # Power-profile bar chart at signature durations (W/kg if weight known else W)
    sig = [(5, "5s"), (60, "1m"), (300, "5m"), (1200, "20m"), (3600, "1h")]
    sig_vals = []
    for d, label in sig:
        if d in pdc.index and pd.notna(pdc.loc[d, "watts"]):
            sig_vals.append((label, int(pdc.loc[d, "watts"])))

    # Reference values for an experienced amateur (Coggan power profile midpoints)
    reference = {"5s": 16.0, "1m": 8.0, "5m": 4.6, "20m": 4.0, "1h": 3.7}  # W/kg
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[s[0] for s in sig_vals], y=[s[1] for s in sig_vals],
        marker_color="#fc4c02", name="你的最佳",
        hovertemplate="%{y} W<extra></extra>",
    ))
    fig.update_layout(template=PLOTLY_TEMPLATE, height=320,
                      yaxis_title="历史最佳功率 (W)",
                      margin=dict(l=50, r=30, t=20, b=40), showlegend=False)

    # Stamina arc: how much your 60-min holds vs mFTP. No in-chart title — the
    # outer panel-title handles labeling so it matches sibling panels.
    stamina_fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=pd_m.stamina if math.isfinite(pd_m.stamina) else 0,
        number={"suffix": "%", "font": {"size": 40}},
        gauge={
            "axis": {"range": [70, 105], "tickwidth": 1, "tickcolor": "#999"},
            "bar": {"color": "#fc4c02"},
            "bgcolor": "white",
            "borderwidth": 0,
            "steps": [
                {"range": [70, 85], "color": "#ffe0d4"},
                {"range": [85, 95], "color": "#fff3e0"},
                {"range": [95, 105], "color": "#e6f4ea"},
            ],
        },
        domain={"x": [0, 1], "y": [0, 1]},
    ))
    stamina_fig.update_layout(height=320, margin=dict(l=20, r=20, t=20, b=20),
                              paper_bgcolor="rgba(0,0,0,0)")

    return render_template("rider_profile.html",
                           profile=profile, m=pd_m,
                           sig_plot=figure_html(fig, "sig-plot"),
                           stamina_plot=figure_html(stamina_fig, "stamina-plot"))


@app.route("/training-load")
def training_load():
    pd_m = _pd_model_cache()
    ftp = pd_m.mftp or 200
    rides = _rides_with_tss(get_rides(), ftp)
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
        yaxis=dict(title="CTL / ATL / TSB(TSS/天)", zeroline=True, zerolinewidth=1),
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
def zones():
    pd_m = _pd_model_cache()
    ftp = pd_m.mftp or 200

    # Aggregate time-in-zone across all rides (sample-based count)
    totals = {name: 0 for name, _, _, _ in wko.COGGAN_ZONES}
    recent = {name: 0 for name, _, _, _ in wko.COGGAN_ZONES}
    cutoff_ts = (pd.Timestamp.now("UTC").tz_localize(None) - pd.Timedelta(days=90)).tz_localize("UTC")

    rides = get_rides()[["id", "start_date", "device_watts"]]
    rides = rides[rides["device_watts"] == 1]
    for _, r in rides.iterrows():
        streams = wko.load_streams(int(r["id"]))
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
        marker_color=[z["color"] for z in zones_meta],
        name="全部时间",
        hovertemplate="%{y:.1f}%<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=[z["name"] for z in zones_meta],
        y=[recent_pct[z["name"]] for z in zones_meta],
        marker_color=[z["color"] for z in zones_meta],
        marker_pattern_shape="/",
        name="近 90 天",
        hovertemplate="%{y:.1f}%<extra></extra>",
    ))
    fig.update_layout(template=PLOTLY_TEMPLATE, height=460, barmode="group",
                      yaxis_title="占运动时间比例 (%)", margin=dict(l=50, r=30, t=30, b=80))

    return render_template("zones.html",
                           plot=figure_html(fig, "zones-plot"),
                           zones_meta=zones_meta,
                           ftp=int(ftp),
                           total_pct=total_pct, recent_pct=recent_pct,
                           totals=totals, recent=recent)


@app.route("/rides")
def rides_list():
    rides = get_rides()
    rides = rides[rides["sport_type"].isin(wko.CYCLING_SPORT_TYPES)].copy()
    rides = rides.sort_values("start_date", ascending=False).reset_index(drop=True)
    return render_template("rides.html", rides=rides.to_dict(orient="records"))


@app.route("/ride/<int:activity_id>")
def ride_detail(activity_id: int):
    pd_m = _pd_model_cache()
    ftp = pd_m.mftp or 200
    streams = wko.load_streams(activity_id)
    if not streams:
        abort(404)

    conn = __import__("storage").connect()
    row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    info = dict(row)

    watts = streams.get("watts", np.array([]))
    hr = streams.get("heartrate")
    cadence = streams.get("cadence")
    altitude = streams.get("altitude")
    time_s = streams.get("time", np.arange(watts.size))

    np_w = wko.normalized_power(watts) if watts.size else float("nan")
    ts = wko.tss(np_w, ftp, info.get("moving_time") or 0)
    tiz = wko.time_in_zones(watts, ftp) if watts.size else {}
    wbal = wko.wbal_skiba(watts, pd_m.cp_raw, pd_m.w_prime) if watts.size and pd_m.cp_raw else np.array([])

    # Composite time-series figure: power + W'bal + HR (multi-axis)
    fig = go.Figure()
    if watts.size:
        # Smooth power for readability (30s rolling)
        smooth = pd.Series(watts).rolling(30, min_periods=1).mean()
        fig.add_trace(go.Scatter(x=time_s, y=smooth, name="功率(30 秒平滑)",
                                 line=dict(color="#fc4c02", width=1.2)))
    if wbal.size:
        fig.add_trace(go.Scatter(x=time_s, y=wbal / 1000, name="W′ 余量 (kJ)",
                                 line=dict(color="#1976d2", width=1.4), yaxis="y2"))
    if hr is not None and hr.size:
        fig.add_trace(go.Scatter(x=time_s, y=hr, name="心率 (bpm)",
                                 line=dict(color="#e53935", width=1), yaxis="y3"))
    fig.update_layout(
        template=PLOTLY_TEMPLATE, height=560,
        yaxis=dict(title="功率 (W)", side="left"),
        yaxis2=dict(title="W′ 余量 (kJ)", overlaying="y", side="right", anchor="x"),
        yaxis3=dict(title="心率", overlaying="y", side="right", position=0.97, showgrid=False),
        xaxis_title="时间(秒,从起点)",
        margin=dict(l=60, r=80, t=20, b=50),
        legend=dict(orientation="h", y=1.05, x=0),
    )

    # Zone bar
    zfig = go.Figure()
    zones_meta = [{"name": n, "color": c} for n, _, _, c in wko.COGGAN_ZONES]
    secs = [tiz.get(z["name"], 0) for z in zones_meta]
    zfig.add_trace(go.Bar(
        x=[z["name"] for z in zones_meta], y=secs,
        marker_color=[z["color"] for z in zones_meta],
        hovertemplate="%{x}: %{y:.0f}s<extra></extra>",
    ))
    zfig.update_layout(template=PLOTLY_TEMPLATE, height=300,
                       yaxis_title="秒数", margin=dict(l=50, r=30, t=20, b=80))

    # W'bal minimum (lowest point of anaerobic reserve during the ride)
    wbal_min_kj = float(wbal.min() / 1000) if wbal.size else None
    wbal_min_pct = (wbal_min_kj / pd_m.frc_kj * 100) if (wbal_min_kj is not None and pd_m.frc_kj) else None

    return render_template(
        "ride_detail.html",
        info=info,
        np_w=np_w,
        intensity_factor=(np_w / ftp) if (ftp and not math.isnan(np_w)) else None,
        tss=ts,
        tiz=tiz, fmt_secs=fmt_secs,
        plot=figure_html(fig, "ride-plot"),
        zone_plot=figure_html(zfig, "zone-plot"),
        wbal_min_kj=wbal_min_kj,
        wbal_min_pct=wbal_min_pct,
        ftp=int(ftp),
        zones_meta=zones_meta,
    )


@app.route("/race-strategy", methods=["GET", "POST"])
def race_strategy():
    """Two modes:
       1) Estimate from race distance + terrain (no upload needed).
       2) Upload a GPX route file for course-aware per-segment planning.
    """
    import route as route_mod  # avoid name clash with Flask `request`

    pd_m = _pd_model_cache()
    distance_km = float(request.values.get("distance_km") or 40)
    terrain = request.values.get("terrain", "rolling")
    weight_kg = float(request.values.get("weight_kg") or 75)
    cda = float(request.values.get("cda") or 0.32)
    intensity_bias = float(request.values.get("intensity_bias") or 1.0)

    # Total system mass = rider + bike (8 kg). Allow override via form later if useful.
    system_mass = weight_kg + 8.0

    course = None
    course_error = None
    course_warning = None
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
        # Flag suspicious / empty elevation data so the user isn't misled.
        if course.max_ele == course.min_ele and course.total_km > 5:
            course_warning = (
                "此路书未包含海拔数据(所有点的 ele 均为 0),"
                "模型只能按平路处理。如实际道路有起伏,完赛时间和分段功率"
                "可能与真实情况差异较大。可尝试在 RideWithGPS、Strava 路线"
                "或 Komoot 重新导出带高程的 GPX。"
            )
        elif course.total_km > 20 and course.total_elev_gain < 20:
            course_warning = (
                "路书海拔变化极小,可能是平面规划导出。请确认数据来源。"
            )

        plan = wko.plan_course(course, pd_m,
                               weight_kg=system_mass, cda=cda,
                               intensity_bias=intensity_bias)
        # Only surface climbs that hit at least Cat 4 — the rest are noise.
        all_climbs = route_mod.find_climbs(course)
        climbs = [c for c in all_climbs if c.category != "—"]
        # Elevation profile + target-power overlay
        ele_km = [p.distance_m / 1000.0 for p in course.points]
        ele_m = [p.ele for p in course.points]
        seg_km = [(s.start_km + s.end_km) / 2 for s in plan.segments]
        seg_w = [s.target_power_w for s in plan.segments]
        seg_wbal = [s.wbal_pct for s in plan.segments]
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=ele_km, y=ele_m, name="海拔 (m)",
            line=dict(color="#888", width=1), fill="tozeroy",
            fillcolor="rgba(140,140,140,0.18)",
            hovertemplate="%{x:.1f} km · %{y:.0f} m<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=seg_km, y=seg_w, name="目标功率 (W)",
            line=dict(color="#fc4c02", width=2), yaxis="y2",
            hovertemplate="%{x:.1f} km · %{y:.0f} W<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=seg_km, y=seg_wbal, name="W′ 余量 (%)",
            line=dict(color="#1976d2", width=1.5, dash="dot"), yaxis="y3",
            hovertemplate="%{x:.1f} km · %{y:.0f}%<extra></extra>",
        ))
        fig.update_layout(
            template=PLOTLY_TEMPLATE, height=460,
            xaxis_title="距离 (km)",
            yaxis=dict(title="海拔 (m)", side="left"),
            yaxis2=dict(title="目标功率 (W)", overlaying="y", side="right"),
            yaxis3=dict(title="W′ 余量 (%)", overlaying="y", side="right",
                         position=0.97, range=[0, 105], showgrid=False),
            legend=dict(orientation="h", y=1.06, x=0),
            margin=dict(l=60, r=90, t=30, b=50),
        )
        profile_plot_html = figure_html(fig, "course-plot")
    else:
        # No GPX uploaded — synthetic course based on distance + terrain.
        plan = wko.plan_by_distance(distance_km, pd_m, weight_kg=system_mass,
                                    cda=cda, terrain=terrain)
        # Simple companion plot: target power vs distance for several event lengths
        kms = [10, 20, 30, 40, 60, 80, 100, 120, 160, 200]
        rows = []
        for km in kms:
            p = wko.plan_by_distance(km, pd_m, weight_kg=system_mass,
                                      cda=cda, terrain=terrain)
            rows.append({"km": km, "avg_w": p.avg_power_w,
                         "pct": p.avg_power_w / pd_m.mftp * 100 if pd_m.mftp else 0,
                         "time_h": p.total_time_s / 3600,
                         "tss": p.expected_tss})
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
        m=pd_m,
        plan=plan,
        course=course,
        course_error=course_error,
        course_warning=course_warning,
        climbs=climbs,
        distance_km=distance_km,
        terrain=terrain,
        weight_kg=weight_kg,
        cda=cda,
        intensity_bias=intensity_bias,
        plot=profile_plot_html,
        fmt_secs=fmt_secs,
    )


# =====================================================================
#                      HELPERS
# =====================================================================

def _rides_with_tss(rides: pd.DataFrame, ftp: float) -> pd.DataFrame:
    """Compute / fill TSS column. Uses weighted_average_watts (NP) from Strava
    when present; falls back to streams. Cheap and cached implicitly via load_rides."""
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
    print("Wilson 的 AI 教练已启动:http://127.0.0.1:5001")
    app.run(host="127.0.0.1", port=5001, debug=False)
