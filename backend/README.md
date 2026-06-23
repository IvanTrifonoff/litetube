# Litetube — promo proxies for SmartTube fork

Promo proxies as a service. Sells per-month subscription for Russian
HTTPS/SOCKS5 proxies through which SmartTube fork streams YouTube
without ads (RU traffic is not monetized by YouTube's ad-server).

## Architecture (deploy shape)

```
                    [ browser OR SmartTube-TV ]
                          │     │
                HTTPS 443 │     │ TLS+SOCKS5 / TLS+HTTP-CONNECT
                          ▼     ▼
                ┌───────────────────────────┐
                │   host nginx   (:443)     │ ← TLS terminator + WAF
                │   certbot LE              │   fail2ban jails active
                └──────────┬────────────────┘
              proxy_pass   │                    ↓ stream/L4
                           ▼                    ▼
             ┌────────────────────┐    ┌────────────────────┐
             │  FastAPI           │    │  3proxy            │
             │ 127.0.0.1:9090     │    │ 0.0.0.0:11001/11002│
             │ Docker network=host│    │ Docker network=host│
             │ uvicorn 1 worker   │    │ supervisord parent │
             └──────────┬─────────┘    └─────────┬──────────┘
                        │   SIGHUP via PID file  │
                        └─────────────────────-─┘
                                  │
                                  ▼
                ┌────────────────────────────────┐
                │  SQLite WAL @ /srv/proxy-infra │
                │   db schema: users, proxies,   │
                │   payments, bans, api_tokens   │
                └────────────────────────────────┘
```

## Components

| Path | Role |
|---|---|
| `api/`              | FastAPI app (auth, billing, /proxy/refresh, admin) |
| `api/main.py`       | App entry, runs `uvicorn` CLI on startup |
| `api/db.py`         | SQLite + WAL + versioned migrations |
| `api/auth.py`       | JWT, bcrypt, trial-banning, admin actions |
| `api/billing.py`    | BillingProvider abstraction (Mock + Robokassa) |
| `api/proxy_3proxy.py` | 3proxy cfg render + SIGHUP-safe reload queue |
| `api/health_checker.py` | asyncio task probing proxy TCP/HTTP |
| `api/static/`       | Vanilla HTML/JS UIs for client and operator |
| `proxy-pool/`       | 3proxy container (alpine + supervisord + 3proxy) |
| `nginx/`            | vhost configs to symlink into sites-enabled |
| `scripts/`          | init_operator, backup_db, certbot_run |
| `docker-compose.yml`| Orchestration: fastapi + 3proxy, both host-network |
| `setup.sh`          | Boots everything from a fresh clone |
| `.env.template`     | Placeholder env; copy to `.env` and fill secrets |

## Quickstart (deploy on 82.202.141.81)

```bash
ssh admssh@82.202.141.81            # via your default key

sudo -i                              # or operate as a user with sudo
mkdir -p /srv/proxy-infra
cd /srv/proxy-infra

# Bring code from local repo (rsync, git push, scp — your choice).
# Then:
cp .env.template .env
openssl rand -hex 32 >> /tmp/jwt.placeholder    # paste into JWT_SECRET later
# Run init to generate operator bcrypt hash and stash creds:
./scripts/init_operator.py
# (it prints OPERATOR_PASSWORD_HASH that you paste into .env)

./setup.sh
# This:
#  - mkdir /var/www/litetube-acme
#  - chown root for /srv/proxy-infra
#  - docker compose build
#  - docker compose up -d
#  - symlinks 3 nginx vhosts into /etc/nginx/sites-enabled
#  - nginx -t && systemctl reload nginx
#  - runs scripts/certbot_run.sh (issues 3 LE certs)
#  - sets up daily db backup cron
```

After setup, hit `https://litetube.trfnv.ru/` in browser — you should be
on the Litetube landing page; `/admin` opens the operator login.

## Endpoints

### Client (browser)
- `POST /api/auth/signup` — `{email, password}`
- `POST /api/auth/login`  — `{email, password, remember_me?}`
- `POST /api/auth/logout`
- `GET  /api/me`          — `{email, role, status, trial_days_left, paid_until}`
- `GET  /api/proxy/refresh` — `{host, port, login, password, type}`

### Billing
- `POST /api/billing/pay`     — `{currency: "RUB"|"USD"|"EUR"}`
- `POST /api/billing/webhook` — server-to-server callback (no auth)

### Operator (requires `litetube_operator` cookie)
- `POST /api/admin/login`                  — `{email, password}`
- `POST /api/admin/logout`
- `GET  /api/admin/users`
- `GET  /api/admin/users/:id`
- `POST /api/admin/users/:id/ban`          — `{reason}`
- `POST /api/admin/users/:id/unban`
- `POST /api/admin/users/:id/extend_trial` — `{days}`
- `POST /api/admin/users/:id/reset_password` *(returns new password once)*
- `GET  /api/admin/payments`
- `GET  /api/admin/proxies`
- `POST /api/admin/proxies/:uid/retest`
- `POST /api/admin/billing/simulate`       — `{user_id, currency?, amount?}`
- `GET  /api/admin/stats`                  — `{user_counts, proxies}`

### Pages
- `GET /`              — end-user app (signup/login/me/pay/proxy)
- `GET /admin`         — operator console
- `GET /billing/success` — landing after Robokassa pay flow
- `GET /billing/fail`
- `GET /health`        — JSON `{status:"ok"}`

## Maintenance

```bash
# Stream logs
docker compose logs -f fastapi
docker compose logs -f 3proxy
tail -f /var/log/nginx/litetube.trfnv.ru.access.log

# Restart services
cd /srv/proxy-infra && docker compose restart fastapi
cd /srv/proxy-infra && docker compose restart 3proxy
systemctl restart litetube-alerter   # host-side log-watcher daemon

# Backup SQLite db
/srv/backups/proxy-infra/  ← tar+gzip, 30-day rotation, daily @03:00 UTC

# Cert renewal
certbot renew  (already in certbot.timer; ours expires 60d later)

# Alerter health
systemctl status litetube-alerter
tail -f /var/log/litetube/alert_daemon.log
tail -f /var/log/litetube/alerts.jsonl
sqlite3 /srv/proxy-infra/db/litetube.db 'SELECT * FROM alert_state;'
```

## Alerter

External alerting fires on three conditions:

| Signal | When | Architecture |
|---|---|---|
| `proxy_down_5min` | All active proxies report `is_alive=0` with `failed_count≥10` (≈5 min) | In-process lifespan loop (`api/litetube/alert_loops.py:proxy_down_loop`) polls DB every 60s |
| `admin_5xx` | Any `/api/admin/*` ends with 5xx — operator-visible failures | (1) FastAPI middleware (`main.py:admin_5xx_capture`) catches raised + uncaught exceptions; (2) Host-side daemon (`scripts/alert_daemon.py`) tails nginx access logs + uvicorn log via tee — catches it even when FastAPI is the broken component |
| `daily_signup_paid_zero` | UTC day with 0 new users AND 0 completed payments | In-process loop (`alert_loops.py:daily_zero_loop`) runs ~01:00 UTC daily |

### Channels

Three sinks, all configurable; the JSONL log sink is **always on** (defense-in-depth):

- **SMTP** — `ALERT_SMTP_HOST/PORT/USER/PASS/FROM/TLS/TIMEOUT` + `ALERT_EMAIL_TO` (comma-sep).
- **HTTP webhook** — `ALERT_WEBHOOK_URL`. POSTs `{"subject","body","host","iso"}` JSON. Works with Twilio (SMS-via-Twimlet), SMS.ru, Slack incoming, Telegram bot API, Discord, PagerDuty Events API.
- **JSONL log** — appends to `ALERT_LOG_PATH` (default `/var/log/litetube/alerts.jsonl`) and emits a `logger.critical` line. Always-on, never fails.

### Cooldown

State lives in a single SQLite table `alert_state(signal_name PK, incident_active, last_alert_iso, last_alert_value, last_checked_iso)` — so the FastAPI in-process loops AND the host-side daemon cooperatively debounce: a burst of admin-5xx triggers exactly one email, not fifty. Default `ALERT_COOLDOWN_SEC=1800` (30 min) between repeats for the same signal.

### Wiring into logs

- **FastAPI logs**: middleware emits on 5xx in the request path. Plus the lifespan loops are independent.
- **nginx access logs**: `scripts/alert_daemon.py` (systemd: `litetube-alerter.service`) tails `/var/log/nginx/*.access.log` with combined-format regex. Seek-to-EOF on startup prevents replay storm.
- **uvicorn access log**: docker-compose pipes uvicorn through `tee -a /var/log/litetube/uvicorn.log` (host-mounted volume). The daemon tails that file too.

### Disabling the alerter

- Disable installs of the systemd service: remove the unit from `/etc/systemd/system/`.
- Disable in-process only: clear all `ALERT_PROXY_*` / `ALERT_DAILY_*` envs and `ALERT_SMTP_*`+`ALERT_WEBHOOK_URL`; the LogSink still writes to `alerts.jsonl` (graceful degradation).

## Test-mode notes

This deploy is configured for **Robokassa test mode** (`IsTest=1`).
Use `BILLING_PROVIDER=mock` in `.env` to bypass Robokassa entirely
and simulate webhook hits from the operator UI ("Simulate payment"). When
real Robokassa shop is set up, change to `BILLING_PROVIDER=robokassa`,
swap `ROBOKASSA_PASSWORD1/2` for the production values, and set
`ROBOKASSA_TEST_MODE=0`.
