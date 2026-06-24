# Changelog

All notable changes to Litetube are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-06-24

### Added
- Backend: Google Sign-In for clients (Этап 1), feature-flagged via `GOOGLE_AUTH_ENABLED=0` (default off; flip to `1` to activate). Endpoint `POST /api/auth/google` accepts a Google ID token (`{"id_token": …}`), verifies it via the `google-auth` library, and runs the lookup/link/create flow:
  - login by `google_sub` (canonical immutable id) — preserves the row's stored email even if Google rotates it (Workspace rename case);
  - link onto an existing email/password client without overwriting `password_hash` (so users can keep logging in via either flow for Этап 1);
  - create a fresh client row with `password_hash = "!google"` (a sentry value: `bcrypt.checkpw(..., "!google")` raises → login via `/api/auth/login` cleanly fails for Google-only users);
  - rejects operator email collision with 403 `admin_sso_disabled`, conflicting-sub on same email with 409 `google_sub_mismatch`, unverified email claim with 403 `unverified_email`.
- DB migration `SCHEMA_V4`: lightweight `ALTER TABLE users ADD COLUMN google_sub TEXT` + partial unique index `idx_users_google_sub ON users(google_sub) WHERE google_sub IS NOT NULL`. No backfill; existing email/password users keep `google_sub=NULL` and are linked on first Google login.
- New env vars in `.env.template`: `GOOGLE_AUTH_ENABLED` (default `0`) and `GOOGLE_CLIENT_ID` (the OAuth 2.0 Web Client ID; set when flag is on). Production fail-fast: importing `litetube.auth` raises RuntimeError if the flag is on without the client id.
- Backend dependency: `google-auth>=2.30,<3` in `requirements.txt`.
- Tests: 24 new pytest cases in `tests/test_google_auth.py` (feature flag, verify_oauth2_token wrapper classification by exception type incl. network→503, google_login lookup/link/create happy paths, all four rejection cases, endpoint HTTP layer including oversize-token 400 and infra 503, schema-V4 migration sanity). All use a monkeypatched `_google_token_verifier` seam so CI runs offline.

### Security
- New injection seam `_google_token_verifier` in `auth.py` lets ops swap to a dry-run verifier at process start during Google incidents.
- `id_token` length capped at 4096 chars (real Google ID tokens are <2 KB — cap is DoS defence).
- `_google_token_verifier` wrapper classifies network/infra failures as 503 `google_unreachable` rather than 401 (operator sees "Google is down", not "your token is invalid").
- Same `litetube_client` cookie used for both flows; reusing the cookie means reusing the same JWT secret and the existing `client_required` dependency — no authentication-bypass risk introduced by switching flow.

### Notes
- Этап 1 is invisible (`404`) when `GOOGLE_AUTH_ENABLED=0`. Flip via env + restart, no schema change.
- Этап 2/3 (full shadow-table rebuild making `password_hash` nullable; UI Google-Sign-In button) follow in subsequent releases.
- TODO before flipping the flag in production: set `client_max_body_size 32k` on the `litetube.trfnv.ru` nginx vhost (Starlette has no default body-size cap).
- Этап 2 (commit `adce0f0`): `users.password_hash` now nullable via shadow-table rebuild. See notes in `db.py:SCHEMA_V5` for early/late-window failure modes and recovery. Standalone `downgrade_to_v5_to_v4(path)` for emergency rollback.
- Этап 3 (this release): Google Identity Services button wired into `activate.html` and `client.html` next to the email/password tabs. `{{GOOGLE_SIGNIN_BLOCK}}` placeholder is substituted by `main._render()` with a runtime check on `GOOGLE_AUTH_ENABLED` + `GOOGLE_CLIENT_ID`. The render cache key now includes both, so feature-flag flips without process restart still re-render correctly. CDN `accounts.google.com/gsi/client` unreachable in some regions — `onerror` fallback panel shows "Google Sign-In временно недоступен — используйте email и пароль" when the GIS library fails to load.

## [0.1.0] - 2026-06-23

### Added
- Backend: FastAPI app with email/password auth, JWT (HS256) cookies, /api/auth/{signup,login,logout}.
- Backend: trial/active/expired/banned user lifecycle with auto-expiry + Robokassa billing webhook.
- Backend: 3proxy dynamic config regeneration — each user gets unique `<uid>:CL:<auth_token>` SOCKS5/HTTP proxies.
- Backend: device activation via 6-digit short-code pairing (TV mints, phone consumes; one-shot JWT hand-off).
- Backend: operator admin (login, ban/unban, payments, extend trial, reset password, stats).
- Backend: host-side alerter daemon (systemd unit) tails nginx + uvicorn logs with cooldown-shared state.
- Backend: per-IP rate-limit middleware + device-start per-IP rate-limit.
- Frontend: client.html (landing/login/signup/dashboard/trial-states/payment), activate.html (TV pairing), /app APK list page.
- Frontend: Google-Identity-Services-friendly markup, but real Google Sign-In is *not* wired in this release.
- Infra: Docker Compose stack (litetube-api + litetube-3proxy multi-stage build), nginx vhosts for litetube.trfnv.ru / admin.* / api.*, Let's Encrypt via certbot.
- CI/CD: GitHub Actions workflow (validate: py_compile + bash -n + pytest; deploy: SSH git pull + setup.sh; smoke-test after deploy).
- Smoke-test: end-to-end HTTPS for all 3 domains via curl -L --retry --connect-to, validates LE cert.
- Tests: 55 pytest cases (auth flow, billing signatures + webhook, device-claim DB + API).
- Health endpoint exposes build version; HTML footers show `Litetube v0.1.0` (substituted via `_render()`).
- Canonical version string lives in `backend/api/litetube/__init__.py` (`__version__`).
- Git tag `v0.1.0` marks this release.

### Notes
- Versioning + CHANGELOG introduced in this release — future bumps follow Keep a Changelog sections (`Added / Changed / Fixed / Security / Removed / Deprecated`).
- Operator accounts still use email/password; all future client sign-in will support Google.
