# Litetube

Subscription YouTube client without ads — Android TV fork of
[yuliskov/SmartTubeNext](https://github.com/yuliskov/SmartTubeNext) backed by a
self-hosted `proxy-infra` FastAPI/SQLite service that hands out per-user
3proxy credentials and bills via Robokassa.

> 100% Russian UI, 199 ₽ / $3.99 / €3.99 per month after a 3-day free trial.

## Repository layout

```
litetube/
├── backend/                 Litetube FastAPI/SQLite + 3proxy operator logic.
│                            Mirror of proxy-infra/ — public, deployable.
├── tv-fork-patches/         Files to overlay on a clean yuliskov/SmartTubeNext
│                            checkout to produce the Litetube TV flavor
│                            (`stlitetube`, applicationId = app.litetube.tv).
├── docs/                    Standalone docs (activation flow, deployment,
│                            Litetube ↔ 3proxy integration).
├── README.md                This file.
├── LICENSE                  MIT, inherited from SmartTubeNext.
└── .gitignore
```

## What each piece does

### `backend/`

- `api/litetube/main.py` — FastAPI app: client signup/login,
  `/api/devices/start` + `/api/devices/poll` + `/api/devices/claim/complete`
  (TV pairing flow), `/api/proxy/refresh` + `/api/proxy/pool`,
  `/api/billing/pay` + `/api/billing/webhook` (Robokassa hookup).
- `api/litetube/db.py` — SQLite WAL layer with versioned migrations;
  schema v3 adds `device_claims` for the TV-activation round-trip.
- `api/litetube/proxy_3proxy.py` — per-user 3proxy allocation + SIGHUP-safe
  `reload_worker()` that regenerates `3proxy.cfg` from active users.
- `api/litetube/alerter.py` + `alert_loops.py` — cross-process cooldown on
  `admin_5xx`, `proxy_down_5min`, `daily_signup_paid_zero`.
- `nginx/litetube.trfnv.ru.conf` + friends — TLS-terminating vhosts behind
  Let's Encrypt.
- `proxy-pool/Dockerfile` + `entrypoint.sh` — 3proxy container, host
  network, SIGHUP-reload-friendly, dummy-password bootstrap on first run.
- `scripts/init_operator.py` — `docker compose exec`-based operator
  bootstrap (no host-side Python needed).
- `scripts/alert_daemon.py` — host-side systemd daemon that tails nginx
  and uvicorn access logs for `/api/admin/*` 5xx events.
- `setup.sh` — turn-key install on a Debian/Ubuntu VPS (Let's Encrypt +
  nginx + Litetube + 3proxy + operator + alerter timer).
- `docker-compose.yml` — `fastapi` + `3proxy` services, host PID namespace
  so SIGHUP reaches 3proxy from inside FastAPI.
- `.env.template` — every tunable with sane defaults.

### `tv-fork-patches/`

The complete delta against a clean `yuliskov/SmartTubeNext` checkout to
build the Litetube TV client:

- `SharedModules/constants.gradle` + `MediaServiceCore/SharedModules/constants.gradle`
  — `minSdkVersion` 17 → 21.
- `smarttubetv/build.gradle` — new `stlitetube` productFlavor
  (`app.litetube.tv`) with `LITETUBE_API_BASE` and
  `LITETUBE_DEVICE_START_TIMEOUT_SEC` buildConfigFields.
- `smarttubetv/src/main/AndroidManifest.xml` — registers
  `LitetubeActivationActivity`.
- `smarttubetv/src/main/java/.../common/litetube/LitetubePrefs.kt`
  + `LitetubeApi.kt` — JWT + activation-code storage and OkHttp helpers
  hitting `/api/devices/*` and `/api/proxy/pool`.
- `smarttubetv/src/main/java/.../tv/ui/litetube/LitetubeActivationActivity.kt`
  + `res/layout/activity_litetube_activation.xml` — first-launch screen
  with QR + 6-digit code, long-polls the backend.
- `smarttubetv/src/main/java/.../tv/ui/main/SplashActivity.java`
  + `MainApplication.java` — gated bootstrap that forwards to the
  activation screen on the `stlitetube` flavor when JWT is absent.
- `smarttubetv/src/main/res/values/strings.xml` — nine `litetube_activation_*`
  resources + Russian text.

### `docs/`

- `docs/activation-flow.md` — the canonical description of the TV ↔ web
  pairing round-trip, JWT lifecycle, banned/expired UX, and the
  `device_claims` invariants. The same content lives at the bottom of
  `PROJECT_CONTEXT.md` under «Litetube Activation Flow».

## Quick start (development)

### 1. Backend

```bash
cd backend
cp .env.template .env
openssl rand -hex 64 > /tmp/jwt
sed -i "s/<REPLACE_WITH_OPENSSL_RAND_HEX_32>/$(cat /tmp/jwt)/" .env
./scripts/init_operator.py    # creates the operator user inside the FastAPI container
./setup.sh                    # nginx + Let's Encrypt + compose up
```

Public URLs:
- `https://litetube.trfnv.ru/` — клиентский кабинет
- `https://litetube.trfnv.ru/admin/` — операторская консоль
- `https://api.litetube.trfnv.ru/api/...` — API для TV-клиента

### 2. TV client

```bash
git clone https://github.com/yuliskov/SmartTubeNext.git upstream-st/
cd upstream-st
git submodule update --init
cp -R ../tv-fork-patches/* .
./gradlew installStstableDebug   # build the upstream flavor
./gradlew assembleStlitetube     # build the Litetube flavor specifically
```

The Litetube APK lands in
`smarttubetv/build/outputs/apk/stlitetube/debug/app-litetube-tv-debug.apk`
(applicationId `app.litetube.tv`).

## License

MIT — inherited from
[yuliskov/SmartTubeNext](https://github.com/yuliskov/SmartTubeNext).
Copyright (c) 2020-present yuliskov for the PlaybackCore / SmartTube parts.
Litetube-specific additions: 2026.
