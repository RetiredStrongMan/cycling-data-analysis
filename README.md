# Wilson's AI Coach — Strava data pipeline

Local-only Strava ingestion + analysis. Single user, your data, no server required.

## What's here

| File | Role |
|---|---|
| `exchange_code.py` | One-time: trade the OAuth `code` from your redirect URL for a `refresh_token`. |
| `strava.py` | Shared client: token refresh, rate-limit-aware GETs. |
| `storage.py` | SQLite + per-ride JSON streams. Schema is permissive — full payload kept in a `raw` column. |
| `backfill.py` | Paginate all activities, then pull streams for cycling rides. Idempotent — safe to re-run. |
| `sync.py` | Incremental: fetch only activities added/edited since last run. |
| `inspect_data.py` | Quick sanity check / stats. |
| `analysis.py` | Reusable analytics: PDC, FTP, NP, TSS, weekly rollups, HR decoupling. |
| `report.py` | CLI report → `reports/summary.txt`, `power_duration_curve.png`, `weekly_volume.png`. |
| `wko.py` | WKO5-style metrics: mFTP, FRC, TTE, W′bal, rider phenotype, PMC, pacing. |
| `app.py` + `templates/` + `static/` | Flask web app — interactive Plotly dashboard. |
| `run.sh` | One-shot: sync + launch web app + open browser. |

Data lands in `data/rides.db` and `data/streams/{id}.json`. Both are gitignored.

## Setup (one-time)

1. **Make `.env`**
   ```bash
   cp .env.example .env
   ```
   Open `.env` and paste your client secret next to `STRAVA_CLIENT_SECRET=` — never share this value.

2. **Install deps** (Python 3.10+)
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
   If pip errors with `CERTIFICATE_VERIFY_FAILED`, the Python.org installer's CA bundle is missing. Bypass with trusted-host flags:
   ```bash
   pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt
   ```
   The `requests` library uses certifi internally, so Strava API calls work fine regardless.

3. **Exchange the OAuth code for a refresh token**
   You should already have a code from the redirect URL (e.g. `?code=f071…`). The code is single-use and expires within ~10 min — if expired, get a new one by visiting:
   ```
   https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&approval_prompt=force&scope=activity:read_all,profile:read_all,read&redirect_uri=http://localhost
   ```
   Then run:
   ```bash
   python exchange_code.py <THE_CODE>
   ```
   This writes `STRAVA_REFRESH_TOKEN` into `.env`. From now on every script reads it from there and refreshes the access token automatically.

## Backfilling history

```bash
python backfill.py                 # summaries + cycling streams (default)
python backfill.py --summaries     # just paginate the list (fast)
python backfill.py --streams       # only pull streams for known rides
python backfill.py --details       # also pull per-ride detail (NP, splits, laps…)
python backfill.py --all-sports    # don't restrict streams to cycling
```

The script reads Strava's `X-RateLimit-Usage` / `X-RateLimit-Limit` headers after every call. When you hit ~90% of the 15-min window it sleeps to the next quarter-hour. When daily usage hits ~97% it exits cleanly — re-run after UTC midnight and it picks up where it stopped.

Rate-limit math: defaults are **100 reads / 15 min** and **1,000 reads / day**. One ride = 1 stream call (plus 1 detail call if you use `--details`). So ~500–1000 rides per day is the realistic cap; multi-day backfill is normal for big histories.

## Incremental updates

```bash
python sync.py            # since last successful run (14d window on first run)
python sync.py --days 30  # explicit window
```

Run manually, or wire to cron / launchd / a calendar event. At hourly cadence this is ~24 calls/day.

## Sanity check

```bash
python inspect_data.py
```

Prints totals, sport breakdown, date range, and the 5 most recent rides.

## What's stored

`activities` table — one row per activity. Columns:
- Identity / time: `id`, `start_date`, `start_date_local`, `timezone`
- Effort: `distance`, `moving_time`, `elapsed_time`, `total_elevation_gain`
- Speed: `average_speed`, `max_speed`
- Power: `average_watts`, `max_watts`, `weighted_average_watts` (NP, detail-only), `kilojoules`, `device_watts` (1 = real power meter)
- HR: `has_heartrate`, `average_heartrate`, `max_heartrate`
- Other: `suffer_score`, `trainer`, `commute`, `polyline`, `summary_polyline`
- `raw` — full JSON payload, so any field Strava returns is recoverable without a re-fetch
- `detail_fetched_at`, `streams_fetched_at` — provenance flags

`data/streams/{id}.json` — per-ride time-series. Default keys: `time, distance, latlng, altitude, velocity_smooth, heartrate, cadence, watts, temp, moving, grade_smooth`. Use these for power-duration curves, HR decoupling, climb categorization, heatmaps, etc.

## Web app (recommended day-to-day)

```bash
./run.sh                  # syncs new rides, launches app, opens browser
./run.sh --no-sync        # just launch the app without polling Strava
```

Then open http://127.0.0.1:5001/. Sections:

- **Dashboard** — mFTP, FRC, TTE, Pmax, Stamina, Rider Type, CTL/ATL/TSB at a glance.
- **Power Curve** — interactive Power-Duration Curve with Monod CP fit, mFTP/TTE markers, and best-effort table at key durations.
- **Rider Profile** — phenotype classification (Sprinter / Pursuiter / TT / All-Rounder / Climber) with the ratios driving it.
- **Training Load** — Performance Management Chart (Banister CTL/ATL/TSB EWMAs) over your full history.
- **Zones** — Coggan 7-zone time distribution, all-time vs last 90 days.
- **Rides** — full activity list, click any to drill in.
- **Ride detail** — power + W′bal + HR overlay, time-in-zones, NP/IF/TSS, anaerobic reserve low-point.
- **Race Strategy** — target power for a given event duration + terrain, based on your modeled physiology.

The metrics implemented (WKO5-inspired; exact WKO formulas are proprietary):

| Metric | Definition | How it's computed |
|---|---|---|
| mFTP | Modeled functional threshold power | CP from Monod-Scherrer fit × 0.97 |
| FRC | Functional Reserve Capacity (kJ) | W′ from the same fit ÷ 1000 |
| TTE | Time to Exhaustion at mFTP | Longest duration the actual PDC sits at/above mFTP |
| Pmax | Peak neuromuscular power | All-time best 1-second power |
| Stamina | 60-min best ÷ mFTP × 100 | Higher = better fatigue resistance |
| W′bal (DFRC) | Anaerobic reserve, second-by-second | Skiba 2012 integral model with empirical τ_w |
| TSS / NP / IF | Standard Coggan formulas | NP = 30s-roll-mean → 4th-power-mean → 4th-root |
| CTL / ATL / TSB | Banister-style EWMAs | 42-day / 7-day; TSB = CTL − ATL |

## Analysis

```bash
python report.py                       # writes reports/summary.txt + 2 PNGs
python report.py --since 2026-01-01    # YTD only
python report.py --ftp 215             # override the auto-estimated FTP
```

The estimator uses your all-time best 20-minute power × 0.95. Override with `--ftp` if you have a fresher number from a recent test.

Programmatically:
```python
import analysis
df = analysis.load_rides(only_with_power=True)
pdc = analysis.power_duration_curve()              # best mean-max per duration
ftp = analysis.estimate_ftp(pdc)
df = analysis.add_tss_columns(df, ftp=ftp)
weekly = analysis.weekly_summary(df)
acr = analysis.acute_chronic_ratio(weekly)         # overtraining canary
decouple = analysis.hr_decoupling(activity_id=123) # aerobic stress per ride
```

## Troubleshooting

- **`401 Unauthorized`** — refresh token rejected. Re-do the OAuth exchange (`exchange_code.py`) to get a new one. This usually happens if you deauthorized the app from Strava settings.
- **`429 Too Many Requests`** — you blew through the 15-min window. The script handles this by sleeping; if you see it repeatedly, lower the rate by adding a `time.sleep(0.5)` in `strava.py:get`.
- **No private rides showing up** — your scope is missing `activity:read_all`. Re-run the OAuth authorize URL with the right scopes (the one in setup step 3) and exchange again.
- **`device_watts` is 0 / null on most rides** — you don't have a power meter on that bike, or Strava is estimating watts from speed+grade. Estimated power is noisy; treat power-based analytics as approximate for those rides.
