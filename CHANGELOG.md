# Changelog

All notable changes to Litetube are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
