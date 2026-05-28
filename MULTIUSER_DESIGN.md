# Multi-user SaaS — design notes

Plan for turning the current single-user local app into a hosted multi-user
service where each visitor signs in with their own Strava account and gets a
private dashboard of their own data.

## 1. Goals & non-goals

**Goals**
- Anyone with a Strava account can sign in (no separate password).
- Each user sees only their own rides, FTP, PMC, race plans.
- The hosted URL is reachable from the internet on HTTPS.
- Initial backfill happens in the background; the user can leave and come back.
- Subsequent syncs are near-real-time via Strava webhooks.

**Non-goals (for v1)**
- Coach-athlete relationships (one coach viewing many athletes).
- Team / club views.
- Public sharing of individual rides or profiles.
- Mobile-native app.
- Strava activity *upload* — read-only.

## 2. High-level architecture

```
                          ┌────────────────┐
   Browser  ───HTTPS────▶ │  Flask web app │ ◀──── Strava OAuth callback
                          │  (gunicorn)    │ ◀──── Strava webhook callback
                          └─────┬──────────┘
                                │
              ┌─────────────────┼──────────────────┐
              ▼                 ▼                  ▼
       ┌──────────────┐  ┌────────────┐    ┌────────────────┐
       │  PostgreSQL  │  │  Redis     │    │ Object store   │
       │  (users,     │  │  (job      │    │ (per-user ride │
       │  activities, │  │  queue,    │    │  stream JSON)  │
       │  sessions)   │  │  sessions) │    └────────────────┘
       └──────────────┘  └─────┬──────┘
                               │
                      ┌────────▼─────────┐
                      │  Worker process  │
                      │  (RQ / dramatiq) │
                      │  - backfill jobs │
                      │  - webhook sync  │
                      └──────────────────┘
```

**Why this stack vs. the current code**
- SQLite → **Postgres** so the worker process and web process can share
  state safely, and so users don't get serialized through one DB lock.
- Per-ride JSON streams stay on disk (or S3/R2/B2) — they're large and
  immutable, no need for a DB.
- Redis for the background job queue. Avoids the operational weight of
  Celery; RQ or dramatiq is enough.

## 3. Data model (Postgres)

```sql
CREATE TABLE users (
    id                  BIGSERIAL PRIMARY KEY,
    strava_athlete_id   BIGINT      UNIQUE NOT NULL,
    email               TEXT,                                     -- from Strava
    first_name          TEXT,
    last_name           TEXT,
    profile_image_url   TEXT,
    weight_kg           NUMERIC(5,2),
    -- Encrypted at rest with a server-side AEAD key
    refresh_token_enc   BYTEA       NOT NULL,
    -- Latest access token (refreshed on demand; cached here to avoid hitting
    -- /oauth/token on every request)
    access_token_enc    BYTEA,
    access_token_exp    TIMESTAMPTZ,
    last_sync_at        TIMESTAMPTZ,
    backfill_state      TEXT        NOT NULL DEFAULT 'pending',   -- pending|running|done|failed
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deauthorized_at     TIMESTAMPTZ
);

CREATE TABLE activities (
    user_id             BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id                  BIGINT      NOT NULL,                     -- Strava activity id
    -- ... same columns as the existing single-user schema ...
    raw                 JSONB       NOT NULL,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX activities_user_start ON activities(user_id, start_date DESC);

CREATE TABLE sessions (
    sid                 TEXT        PRIMARY KEY,
    user_id             BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at          TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE subscription_state (
    -- Single row holding our Strava push-subscription id (one per app)
    id                  INTEGER PRIMARY KEY DEFAULT 1,
    strava_sub_id       INTEGER,
    callback_url        TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Stream storage**: one file per (user_id, activity_id) under
`streams/{user_id}/{activity_id}.json.gz`. Use S3-compatible object storage
(Cloudflare R2 has a free 10GB tier). For Fly.io persistent volumes are fine
up to a few hundred GB.

## 4. Auth flow

1. Visitor clicks **Sign in with Strava** → redirect to
   `https://www.strava.com/oauth/authorize?...&scope=read,activity:read_all,profile:read_all`.
2. Strava redirects to `/oauth/callback?code=...` on our domain.
3. Server exchanges the code for `access_token` + `refresh_token` + athlete
   payload. Upserts the `users` row. Encrypts tokens with a server key (AES-GCM)
   before storing.
4. Server creates a `sessions` row, sets an HTTP-only signed cookie, redirects
   to `/dashboard`.
5. For every request, middleware reads the session cookie → looks up `user_id`
   → loads the user (token-refresh as needed) → puts on `g.user`.
6. **Sign-out** clears the cookie + deletes the session row.
7. **Account deletion / deauthorization** — when Strava sends a
   `aspect_type=update updates={authorized:false}` webhook, we mark
   `deauthorized_at`, remove tokens, and queue a hard delete of the user's
   data after 24h.

## 5. Background sync

Two parts:

**Initial backfill** — when a user first connects, enqueue a `backfill(user_id)`
job. The worker:
1. Sets `users.backfill_state = 'running'`.
2. Pages `/athlete/activities` (one HTTP call per 200 rides — cheap).
3. For each cycling ride, pulls `/activities/{id}/streams`. This is the
   expensive part — 1 call per ride. At the shared app limit of 1000/day,
   a user with 500 rides takes one day to fully backfill.
4. Writes streams to object storage and metadata to Postgres as each ride
   lands (so the dashboard becomes usable progressively).
5. On completion: `backfill_state = 'done'`, `last_sync_at = now()`.

The dashboard polls `/api/backfill-status` every 5s while running, showing a
progress bar `processed_rides / total_rides`.

**Incremental** — Strava webhooks. On signup we register one app-wide
subscription pointing at `https://OURDOMAIN/strava/webhook`. For each push:
1. Validate the `subscription_id` matches ours.
2. Enqueue `sync_one(user_id, activity_id, aspect_type)`.
3. Respond 200 within 2 seconds (Strava requires this).

The worker fetches the activity detail + streams, upserts. Net cost: 2
API calls per activity per user per upload. Far below the 1000/day budget.

## 6. Strava API rate-limit strategy

The 100/15min and 1000/day limits are **per app**, not per user. With N users
all backfilling, they get serialized.

Mitigations:
- **One global rate-limiter** in Redis (token-bucket). Every API request
  acquires a token; backfill workers block when empty.
- **Apply for higher limits** via Strava's API agreement form. They grant
  10x increases for legitimate analytics apps; you need a privacy policy
  + ToS page + a request form filled out.
- **Prioritize webhooks over backfills** — incremental syncs run on a
  separate queue with priority.

For v1 with say 50 users and Strava sandbox limits, backfills will be slow
(days) under load. That's acceptable as long as the UI explains it. Apply for
production-tier limits before opening signups widely.

## 7. Hosting

**Recommended for v1**: Fly.io.

Pros:
- Persistent volumes (good for stream JSON files + Postgres) on the free tier.
- Built-in HTTPS + custom domains.
- Auto-deploy from GitHub via Fly's GitHub Actions.
- Postgres add-on (or use Supabase / Neon for managed).
- Cheap to scale up.

Single-machine deployment:
```
fly app
├── web    (gunicorn flask app, 1 vm)
├── worker (rq worker, 1 vm)
└── pg     (managed postgres, smallest tier ~$0/mo on Neon)
```

Estimated monthly cost at MVP scale (<100 users):
- Fly compute: $0–$5 (free allowance)
- Postgres on Neon free tier: $0
- Cloudflare R2 storage: $0 (10GB free)
- Domain: ~$12/year

**Production hardening before opening signups**:
- HTTPS-only cookies, `SameSite=Lax`, signed with `SECRET_KEY`.
- CSP headers, rate-limit OAuth callback against brute-force.
- A `/privacy` page and `/tos` page (Strava ToS requires this).
- Sentry or similar for error tracking.
- Backups: nightly `pg_dump` to R2, retain 14 days.

## 8. Code refactoring map

Files to add:
- `auth.py` — OAuth routes (`/login`, `/oauth/callback`, `/logout`), session middleware.
- `models.py` — SQLAlchemy ORM models for users / activities / sessions.
- `worker.py` — RQ entrypoint; defines `backfill(user_id)` and `sync_one(user_id, aid, aspect)`.
- `webhook.py` — Strava webhook receiver + subscription bootstrap.
- `crypto.py` — token encryption (AES-GCM with a key from env).
- `Dockerfile`, `fly.toml`, `.github/workflows/deploy.yml`.

Files to refactor:
- `app.py` → every route filters by `g.user.id`. No more global `_pdc_cache`;
  cache per-user via `functools.lru_cache` keyed on user_id with a small max.
- `storage.py` → Postgres-backed. Keep the same function signatures, just
  swap the implementation.
- `strava.py` → `StravaClient(user)` instead of reading env; refresh writes
  back to `users.refresh_token_enc`.
- `backfill.py`, `sync.py` → become worker job bodies, taking `user_id`.
- `wko.py`, `analysis.py`, `route.py` → unchanged. They already operate on
  data passed in.

Files unchanged: `report.py` (CLI tool — keep as local-only), all templates
(URLs already use `url_for`, just need the auth-gated layout).

## 9. Phased implementation plan

**Phase 1 — local refactor (no deploy yet)**  · 3–5 days
- Move from SQLite to Postgres locally (docker compose).
- Add user table + session middleware. Real Strava OAuth, but the only
  user is still you.
- Verify everything works for one user before scaling to many.

**Phase 2 — multi-user, single-machine**  · 2–3 days
- Worker process + RQ.
- Initial-backfill job with progress UI.
- Webhook receiver (use ngrok or fly preview for dev).

**Phase 3 — production deploy**  · 1–2 days
- Dockerfile + fly.toml.
- Domain + HTTPS.
- Privacy + ToS pages.
- Object storage for streams.
- Sentry, logs, backups.

**Phase 4 — public open**  · ongoing
- Apply for production-tier Strava rate limits.
- Onboarding polish (loading states, empty-state for first-time users).
- Account deletion UX.
- Strava brand guidelines compliance (logo placement, "Powered by Strava").

## 10. Open questions

1. **Domain**: do you have one? (e.g. `coach.wilsongong.com` or similar.)
2. **Strava app**: the current `client_id=251177` is registered as
   "Performance Analysis" with localhost callback. For multi-user we need
   to (a) keep the existing app and just update the callback domain to the
   production URL, or (b) register a new "production" Strava app and run
   the local one in parallel. (b) is safer.
3. **Pricing**: free forever, or eventually paid tiers? This shapes whether
   we need billing infra from day 1.
4. **Geographic reach**: any need to serve users in China specifically? If
   yes, the host needs to be reachable from there (Fly's HK region works).
5. **Coach view**: if you want this for a coaching practice (1 coach ↔ many
   athletes), the data model changes significantly. Worth deciding now.

---

If this plan looks roughly right, the next step is Phase 1: refactor the
existing code to Postgres + add OAuth, with just yourself as the only user.
That's the proof point that the architecture works end-to-end before any
deploy. I'd estimate ~1 day to a working local multi-tenant prototype.
