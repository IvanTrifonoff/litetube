# Litetube Activation Flow

> Canonical description of the TV ↔ web pairing round-trip that ties a
> physical Android TV to a Litetube account. Mirrors §5 of PROJECT_CONTEXT.md;
> kept here so the litetube repo is self-contained.

## 1. Token model — shared JWT key

One `JWT_SECRET` signs all client tokens: web-cookie, mobile-app, and TV-app.
A JWT minted on the TV side via activation is interchangeable with a
session JWT minted in the browser; both are HS256 with payload
`{user_id, role, exp}` and last `JWT_EXPIRY_HOURS` (default 24h).
`auth.client_required` accepts any non-banned, non-expired client token
regardless of which surface minted it.

## 2. Cold start, no stored JWT — round-trip

| # | Caller | Endpoint | Outcome on the TV |
|---|--------|----------|-------------------|
| 1 | TV | `POST /api/devices/start` (anonymous, per-IP `DEVICE_START_PER_IP_PER_MIN=12`) | `{code: "742195", expires_in: 600, qr_url: "https://litetube.trfnv.ru/activate?code=742195"}` |
| 2 | TV | `GET /api/devices/poll?code=742195` every ~2 s, holds up to `DEVICE_POLL_MAX_SEC=30` | 200 `{status:"claimed", jwt, claimed_at}` → write JWT to `LitetubePrefs`, clear stored activation code, re-launch `SplashActivity`; 202 `{status:"pending"}` → keep polling; 410 `{status:"expired"}` → show "restart the app" status |
| 3 | Phone (web) | `POST /api/devices/claim/complete {code:…}` with the user's existing `litetube_client` cookie | Atomic `UPDATE device_claims SET user_id=?, claimed_jwt=?, claimed_ip=?, claimed_ua=?, claimed_at=? WHERE code=? AND user_id IS NULL`; mints a fresh `auth._issue_token(user_id, "client", JWT_EXPIRY_HOURS)` into `claimed_jwt`. Loser of a concurrent race → 409 `race_lost`. Unknown/already-claimed/already-expired codes → 404 / 409 / 410. |
| 4 | TV | writes the JWT, clears cached activation code, `Intent(SplashActivity).addFlags(NEW_TASK | CLEAR_TASK)` | upstream flow continues |
| 5 | TV (after `RussiaProxySelector.bootstrap`) | `GET /api/proxy/pool` with Bearer JWT | Server returns up to `PROXY_POOL_SIZE=3` rows, ordered by `is_alive DESC, latency_ms ASC`. RussiaProxySelector parallel-tests each through OkHttp; fastest-responding one is applied to the system proxy. |

## 3. JWT expiry in MVP

Activation JWT inherits `JWT_EXPIRY_HOURS` (default 24h) — same as web login.

**No proactive refresh** on the TV. When `/api/proxy/pool` returns 401,
`LitetubeApi.fetchProxyPool` returns `null`; RussiaProxySelector falls back
to its locally-cached pool JSON in SharedPreferences until cached entries
also fail.

Recommended follow-up: a `OneTimeWorkRequest` worker that calls `/api/me`
daily; on 401 clear the JWT and forward back to `LitetubeActivationActivity`.

## 4. `device_claims` invariants

- PK: 6-digit numeric string (10⁶ space ⇒ ~0.13 % collision at 50 active
  codes; server retries up to 3 times).
- One-shot consumer: `/api/devices/poll` nulls `claimed_jwt` after the
  first successful read so re-polls can't reuse the secret.
- TTL: `DEVICE_CLAIM_TTL_SEC=600` (10 min). Status `expired` once
  `expires_at_iso < now`.
- Per-IP rate-limit on `/api/devices/claim/complete`: 10/min via
  `auth.rate_limit_check`.
- Indexes: `(expires_at_iso)` for cleanup sweep; `(user_id)` for
  per-user accounting.
- Backlog: no automatic deletion of expired rows. Add
  `DELETE FROM device_claims WHERE expires_at_iso < NOW()` to a periodic
  worker in a follow-up.

## 5. Banned / expired UX

`LitetubeActivationActivity` MUST serve a *re-activation* surface in
addition to "no code yet" — but as a separate state, not stacked on top.
A subscribed-out user pointing a TV at the screen today would just see
the QR + code, log into the web on their phone, and watch the activation
fail server-side (`auth.client_required` returns 403 `user_trial_expired`
or `user_subscription_expired`). That UX is broken.

Recommended UI states:

| When the screen renders | UI state | Action button |
|-------------------------|----------|---------------|
| No JWT in SharedPreferences at all (cold first-launch) | Big QR + 6-digit code, status «Ожидаем подтверждения на телефоне…» | (no button, wait) |
| JWT present but `/api/me` returns 403 `user_banned` / `user_trial_expired` / `user_subscription_expired` | «Подписка истекла / аккаунт заблокирован», «Открыть оплату на телефоне» → `Intent(ACTION_VIEW)` to `https://litetube.trfnv.ru/billing/success` | Clear local JWT before opening the URL; on return, a «Перепроверить» button tails `/api/me`. When 200, restart into `SplashActivity`. |
| JWT valid + `status` ∈ {`trial`, `active`} | Activity should *not* be on screen — `SplashActivity` forwards to `BrowseActivity` immediately. | (no button) |

Concretely:

1. `LitetubeActivationActivity.onCreate()` must call `/api/me` first.
   - 200 → keep behaviour as-is (request a code, render QR).
   - 403 → `LitetubePrefs.clearJwt()` and render the «expired» state.
   - No JWT → skip the `/api/me` call, go straight to `POST /api/devices/start`.
2. The TV MUST NOT bind activation codes to banned/expired users
   server-side — `auth.client_required` already enforces this on
   `/api/devices/claim/complete`.
3. The web `/activate` page (planned but not in MVP) should also branch
   on `/api/me`: if 403, show a «ваш аккаунт приостановлен» CTA before the
   «Привязать ТВ» button.

Phase-2 follow-up; Phase-1 (current MVP) handles only the "no JWT" path.

## 6. Where to read the code in this repo

- `backend/api/litetube/db.py` — `SCHEMA_V3`, ALL_MIGRATIONS=[v1,v2,v3].
- `backend/api/litetube/main.py` — lines for `/api/devices/start`,
  `/api/devices/poll`, `/api/devices/claim/complete`, `/api/proxy/pool`.
- `backend/api/litetube/proxy_3proxy.py` — `proxy_pool()`.
- `backend/.env.template` — `DEVICE_*` + `PROXY_POOL_SIZE` knobs.
- `tv-fork-patches/SharedModules/constants.gradle` and
  `MediaServiceCore/SharedModules/constants.gradle` — `minSdk 21`.
- `tv-fork-patches/smarttubetv/build.gradle` — `stlitetube` flavor.
- `tv-fork-patches/smarttubetv/src/main/java/.../common/litetube/{LitetubePrefs,LitetubeApi}.kt`
  — JWT/QR/poll helpers.
- `tv-fork-patches/smarttubetv/src/main/java/.../tv/ui/litetube/LitetubeActivationActivity.kt`
  + `res/layout/activity_litetube_activation.xml` — first-launch screen.
- `tv-fork-patches/smarttubetv/src/main/java/.../tv/ui/main/{SplashActivity,MainApplication}.java`
  — gate upstream flow when JWT is absent.
