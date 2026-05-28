# Deploying

This app deploys cleanly to any Linux host that runs Docker — the Dockerfile is
platform-agnostic. Two recommended paths:

- **[VPS (Aliyun / DigitalOcean / Oracle Cloud)](#a-vps-deploy-aliyun-hong-kong-recommended-for-china)** — single VM with `docker compose`. Recommended for China users.
- **[Fly.io](#b-flyio-deploy)** — managed PaaS. Simpler ops, ~$3–5 /mo on the Hobby Plan.

Pick one. The codebase supports both — `docker-compose.yml` + `Caddyfile` for
VPS, `fly.toml` for Fly. Neither file affects the other.

---

## A. VPS deploy (Aliyun Hong Kong — recommended for China)

### Why Hong Kong region

Mainland China regions sit behind the GFW; Strava's API (`*.strava.com`) is
blocked from there, so the *server* couldn't reach Strava. The Hong Kong
region (cn-hongkong) is outside the GFW and reachable from both mainland China
users (browser-side OAuth) and the Strava API (server-side). It also doesn't
require ICP filing.

### A.1 Buy / pick a domain

Strava's OAuth requires a hostname (not a raw IP) as the callback domain. The
cheapest options:

- Aliyun 万网 — `.top` or `.icu` ¥9–20/year promotional, `.com` ~¥60/year
- Cloudflare — `.com` from ~$10/year, includes free DNS hosting

Decide what you want the URL to be, e.g. `coach.yourname.com`.

### A.2 Create an Aliyun ECS instance

In the Aliyun console:

1. **ECS → 实例 → 创建实例**
2. **付费方式**: 按量付费 first (to test), switch to 包年包月 later for the
   discount once you're sure
3. **地域**: 香港(可用区 B) — `cn-hongkong-b`
4. **实例规格**: `ecs.t6-c1m2.large` (1 vCPU, 2 GB RAM) is the smallest viable
   spec. ~¥80–120/month at 包年包月 pricing. **Do not** pick 1 GB — the app
   OOMs during PDC fits.
5. **镜像**: Ubuntu 22.04 64-bit
6. **存储**: 系统盘 40 GB ESSD (default tier is fine)
7. **公网 IP**: 分配公网 IPv4 + 选 "按使用流量" (pay-as-you-go bandwidth) —
   cheaper than fixed bandwidth for our traffic levels
8. **带宽峰值**: 5 Mbps is plenty
9. **安全组**: inbound 22 (SSH from your IP only), 80 (any), 443 (any). Outbound: all.
10. **登录凭证**: 选 "密钥对" and upload your SSH public key, OR set a root password
11. Set an instance name like `coach-prod`, create it.

Wait ~30 seconds, then note the **公网 IP**.

### A.3 Point your domain at the VM

In your DNS provider (Aliyun 云解析 or Cloudflare):

- **Type**: A
- **Name**: `coach` (or `@` for apex)
- **Value**: the VM's public IPv4
- **TTL**: 600

Wait 1–2 minutes for propagation (check with `dig coach.yourname.com` or
`nslookup`).

### A.4 SSH in and set up Docker

```bash
ssh root@<VM_PUBLIC_IP>

# Install Docker + Docker Compose plugin
apt-get update && apt-get install -y curl ca-certificates gnupg git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu jammy stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
```

If you're in China and that times out, use the Aliyun Docker mirror instead:

```bash
# Replace the official docker repo line above with Aliyun's mirror:
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://mirrors.aliyun.com/docker-ce/linux/ubuntu jammy stable" \
    > /etc/apt/sources.list.d/docker.list
```

### A.5 Pull the repo + configure secrets

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/RetiredStrongMan/cycling-data-analysis.git coach
cd coach

# Build the .env from the template
cp .env.example .env
nano .env   # or vim
```

Fill in:
```
STRAVA_CLIENT_ID=251177
STRAVA_CLIENT_SECRET=…your value…
STRAVA_REFRESH_TOKEN=                        # leave blank — populated per-user on OAuth
SECRET_KEY=…run `openssl rand -hex 32`…
SESSION_COOKIE_SECURE=1
STRAVA_VERIFY_TOKEN=…run `openssl rand -hex 16`…
DOMAIN_NAME=coach.yourname.com               # your chosen domain
```

### A.6 Update Strava app callback domain

Go to <https://www.strava.com/settings/api> while logged in and edit the app:

- **Authorization Callback Domain**: change from `localhost` to your domain
  (e.g. `coach.yourname.com`). No `http://`, no path, just the hostname.

### A.7 Migrate your existing data (optional)

If you want to bring Wilson's 455 backfilled rides instead of re-fetching:

```bash
# From your laptop, in the project root:
scp -r data/ root@<VM_PUBLIC_IP>:/opt/coach/data-imported

# On the VM:
cd /opt/coach
# (optional) inspect what came over: ls data-imported/streams/1 | wc -l
```

We'll move the data into the docker volume in the next step.

### A.8 First boot

```bash
cd /opt/coach
docker compose build
docker compose up -d
docker compose logs -f app
```

You should see Gunicorn start within a few seconds. Caddy will request a
TLS cert from Let's Encrypt on the first incoming request — let it.

Visit `https://coach.yourname.com`. You should see the login page.

If you SCPd the existing data in step A.7, restore it into the volume:

```bash
docker compose stop app
docker run --rm -v coach_coach_data:/dest -v $(pwd)/data-imported:/src alpine sh -c "cp -r /src/* /dest/"
docker compose up -d app
```

(Volume name is `<project-dir>_<volume-name>`, so `coach_coach_data` if you
cloned into `/opt/coach`. Run `docker volume ls` to confirm.)

### A.9 Sign in and verify

In your browser:

1. Visit `https://coach.yourname.com`
2. Click **Connect with STRAVA** → Strava redirects → grant the three scopes
3. You land on `/backfilling`, then `/dashboard` once backfill completes

If you restored existing data the backfill state should already be `done`
and you go straight to the dashboard.

### A.10 Set up the Strava webhook (incremental sync)

From your laptop (or SSH'd into the VM — either works, just needs the
`.env` with client creds):

```bash
python setup_webhook.py create https://coach.yourname.com/strava/webhook
```

From now on, every activity any of your users uploads, edits, or deletes
triggers a per-user sync within ~2 seconds — no polling needed.

### A.11 Updates

```bash
ssh root@<VM_PUBLIC_IP>
cd /opt/coach
git pull
docker compose build
docker compose up -d        # rolling restart, ~5s downtime
```

### A.12 Backups

```bash
# On the VM:
docker run --rm -v coach_coach_data:/data -v $(pwd):/backup alpine tar czf /backup/coach-$(date +%F).tar.gz -C /data .
# Pull to your laptop:
scp root@<VM_PUBLIC_IP>:/opt/coach/coach-*.tar.gz ./backups/
```

For automated nightly backups, add a cron entry on the VM:

```bash
crontab -e
# Add this line:
0 3 * * * cd /opt/coach && docker run --rm -v coach_coach_data:/data -v /opt/coach/backups:/backup alpine tar czf /backup/coach-$(date +\%F).tar.gz -C /data . && find /opt/coach/backups -name 'coach-*.tar.gz' -mtime +14 -delete
```

### A.13 Tear down

```bash
docker compose down -v       # stops + deletes volumes (DESTROYS DATA)
# Then release the ECS instance in the Aliyun console.
```

---

## B. Fly.io deploy

If you'd rather use Fly than self-manage a VPS — the Dockerfile works
unchanged. See `fly.toml`.

```bash
brew install flyctl
fly auth login
cd "Wilson's AI Coach"
fly launch --no-deploy --copy-config --name YOUR-APP-NAME
fly volumes create coach_data --region hkg --size 1
fly secrets set \
    STRAVA_CLIENT_ID=… STRAVA_CLIENT_SECRET=… \
    SECRET_KEY="$(openssl rand -hex 32)" \
    STRAVA_VERIFY_TOKEN="$(openssl rand -hex 16)"
fly deploy
```

Then update Strava callback domain to `YOUR-APP-NAME.fly.dev` and run
`python setup_webhook.py create https://YOUR-APP-NAME.fly.dev/strava/webhook`.

Fly's Hobby Plan is $5/month minimum as of late 2024.

---

## Apply for higher Strava rate limits

Sandbox tier is 100 reads / 15-min, 1000 / day, **shared across all users on
your app**. With ~10 active users on a normal day this is enough for
incremental sync plus one fresh signup per day. For more, fill out Strava's
production-tier application: <https://www.strava.com/settings/api> → "Request
an increase". Approved apps get 1000/15-min and 20 000/day, ~20× the budget.

## Health + observability

- `/healthz` — Caddy/Fly health-check endpoint (DB ping)
- `docker compose logs -f app` — tail the gunicorn logs
- `docker compose logs -f caddy` — TLS / proxy issues land here
- Errors land in stdout/stderr; capture them with a log shipper (Aliyun SLS,
  Loki, etc.) if you need long retention
