# Deploying to Fly.io

End-to-end runbook to get from local repo → a public HTTPS app on the internet
that anyone can sign in to with their own Strava account.

Target: **Fly.io** in the **hkg** (Hong Kong) region. Free tier viable; the
512 MB Machine costs ~$3/month. Adjust `fly.toml` for other regions.

## 0. Prerequisites

```bash
# Fly CLI
brew install flyctl
fly auth login    # opens browser, sign in or sign up
```

## 1. First deploy

```bash
cd "Wilson's AI Coach"          # the project root

# Create the Fly app + a persistent 1 GB volume for SQLite + stream JSONs.
# Choose your own app name — it becomes <app>.fly.dev. Skip if you already
# edited `app =` in fly.toml.
fly launch --no-deploy --copy-config --name YOUR-CHOSEN-APP-NAME

fly volumes create coach_data --region hkg --size 1
```

If `fly launch` modified your fly.toml in ways you didn't want, revert and
re-run with `--copy-config`.

## 2. Configure secrets

These never enter the git repo or the Docker image — Fly injects them as env
vars at runtime.

```bash
# Required: copy values from your local .env
fly secrets set \
    STRAVA_CLIENT_ID=YOUR_CLIENT_ID \
    STRAVA_CLIENT_SECRET=YOUR_CLIENT_SECRET \
    SECRET_KEY="$(openssl rand -hex 32)" \
    STRAVA_VERIFY_TOKEN="$(openssl rand -hex 16)"
```

`SECRET_KEY` signs the session cookie. If you ever want to invalidate every
user's session, rotate it. `STRAVA_VERIFY_TOKEN` is only used during the
webhook subscription handshake — keep the same value when you call
`setup_webhook.py create` later.

## 3. Update Strava app settings

Go to <https://www.strava.com/settings/api> and edit your app:

- **Authorization Callback Domain**: change from `localhost` to
  `YOUR-CHOSEN-APP-NAME.fly.dev` (no `http://`, no path, just the host).
- **Website**: anything reasonable (e.g. the GitHub repo URL).

Strava's domain match is a substring check on the hostname only — if you keep
both `localhost` and the Fly subdomain working, sign-ins from both URLs will
succeed. Strava only allows one entry per app, so pick the production URL
now; local-dev OAuth will break until you point it back, but you can still
test the local app with `python migrate_to_multiuser.py` + an existing
session cookie.

## 4. Deploy

```bash
fly deploy
```

Takes ~3 minutes the first time (Docker build + push). The Machine boots
automatically. Watch logs:

```bash
fly logs
```

Visit `https://YOUR-CHOSEN-APP-NAME.fly.dev`. You should see the login page.
Sign in with Strava — Strava will redirect back to the Fly URL, the
backfill page appears, and your data starts populating in the background.

## 5. Set up the Strava webhook (incremental sync)

Once the deploy is live and the `/strava/webhook` endpoint is reachable:

```bash
python setup_webhook.py create https://YOUR-CHOSEN-APP-NAME.fly.dev/strava/webhook
```

This POSTs `/push_subscriptions` to Strava. Strava immediately GETs your
callback URL to verify; the app echoes the challenge back; Strava activates
the subscription.

From here, every activity any of your users uploads, edits, or deletes
triggers an automatic per-user sync within seconds — no polling needed.

To inspect / remove:

```bash
python setup_webhook.py list
python setup_webhook.py delete <subscription_id>
```

## 6. Apply for higher Strava rate limits

Sandbox tier is 100 reads / 15-min, 1000 / day, **shared across all users**.
With ~10 active users on a normal day this is enough for incremental sync
plus one fresh signup per day. For more, fill out the Strava production-tier
application: <https://www.strava.com/settings/api> → "Request an increase".

Approved apps get 1000/15-min and 20 000/day, ~20× the budget.

## 7. Monitoring

```bash
fly logs                    # tail
fly logs --instance         # see logs from a specific machine
fly status                  # machine health + IP
fly machine restart         # forced restart (clears worker thread state)
fly ssh console             # interactive shell on the Machine
```

The app exposes `/healthz` which Fly polls every 30s; if it returns non-200
twice in a row Fly automatically restarts the machine.

## 8. Backups

SQLite backups (lightweight):

```bash
# Pull a copy of the live database to your laptop
fly ssh sftp shell
> get /app/data/rides.db ./rides.db.$(date +%F)
> exit
```

For automated backups, add a cron job locally that runs the above. The
streams JSONs in `data/streams/` are also on the volume — restore by copying
them back via the same SFTP shell.

## 9. Updating the deployed app

```bash
git push origin main      # commit + push code
fly deploy                # build + deploy
```

Schema migrations: the `_ensure_columns()` helper in `storage.py` runs on
every connect and is idempotent — add a column there, redeploy, done.

For destructive migrations (renaming columns, dropping data), write a one-
shot script following `migrate_to_multiuser.py` as a template and execute
via `fly ssh console`.

## 10. Cost estimate

| Item | Free tier | This app's usage |
|---|---|---|
| Compute | 3× shared-cpu-1x with 256 MB total | 1× shared-cpu-1x at 512 MB → ~$3/mo |
| Volume | 3 GB total free | 1 GB → free |
| Egress | 160 GB/mo free outbound | <1 GB/mo expected |
| Domain | Free `*.fly.dev` | Free |

Approximate monthly cost for ≤20 active users: **$3**. Bigger machines or
multi-region need scale up `fly.toml` `[[vm]] memory_mb` and / or add more
machines.

## 11. Teardown

```bash
fly apps destroy YOUR-CHOSEN-APP-NAME
# also revoke the webhook so Strava doesn't keep trying to deliver:
python setup_webhook.py list
python setup_webhook.py delete <sub_id>
```
