# Litetube — Operations Runbook

For day-to-day ownership of a deployed Litetube instance.
Companion to [README.md](README.md) (overview) and [CHANGELOG.md](CHANGELOG.md)
(release history). Last updated for release **v0.2.0**.

> All commands assume the host layout from `setup.sh`:
> `/srv/proxy-infra/.env` for env, `/srv/proxy-infra/db/litetube.db` for the
> SQLite DB, nginx vhost root at `/etc/nginx/sites-available/litetube.trfnv.ru.conf`,
> container stack under `docker compose -f /srv/proxy-infra/docker-compose.yml`.

---

## Table of contents

1. [TL;DR — enable Google Sign-In from cold start](#1-tldr--enable-google-sign-in-from-cold-start)
2. [`GOOGLE_CLIENT_ID` — where it lives](#2-google_client_id--where-it-lives)
3. [Hard gates BEFORE `GOOGLE_AUTH_ENABLED=1`](#3-hard-gates-before-google_auth_enabled1)
4. [Smoke-testing after the flag flip](#4-smoke-testing-after-the-flag-flip)
5. [`POST /api/auth/google` failure modes](#5-post-apiauthgoogle-failure-modes)
6. [GIS-reachability fallback UX (accounts.google.com blocked)](#6-gis-reachability-fallback-ux-accountsgooglecom-blocked)
7. [Emergency DB rollback — `downgrade_to_v5_to_v4`](#7-emergency-db-rollback--downgrade_to_v5_to_v4)
8. [Operator-email collision incident](#8-operator-email-collision-incident)
9. [Monitoring after the flip](#9-monitoring-after-the-flip)
10. [Rollback levers — quick reference](#10-rollback-levers--quick-reference)
11. [Env-var quick reference](#11-env-var-quick-reference)

---

## 1. TL;DR — enable Google Sign-In from cold start

```bash
# --- 1. Operator config ---
test -f /srv/proxy-infra/.env || cp /srv/proxy-infra/.env.template /srv/proxy-infra/.env
$EDITOR /srv/proxy-infra/.env
# Set:  GOOGLE_AUTH_ENABLED=1
# Set:  GOOGLE_CLIENT_ID="…apps.googleusercontent.com"

# --- 2. nginx body-size gate (mandatory) ---
$EDITOR /etc/nginx/sites-available/litetube.trfnv.ru.conf
# Inside `server { ... }`, add (or change):
#     client_max_body_size 8k;
# Apply:
nginx -t && systemctl reload nginx

# --- 3. Restart the API so the env picks up + auth.py fail-fast runs ---
cd /srv/proxy-infra && docker compose up -d --force-recreate litetube-api

# --- 4. Sanity ---
curl -fsS -m 5 https://litetube.trfnv.ru/health | jq -r '.version'   # 0.2.0+

# --- 5. Functional smoke ---
curl -fsS -m 5 -X POST -H 'Content-Type: application/json' \
     -d '{"id_token":"<paste a real Google JWT>"}' \
     https://litetube.trfnv.ru/api/auth/google
#   expected: 200 {"token":"…","user_id":N,"created":true|false,"linked":true|false}
```

If step 1 errors with `RuntimeError: Litube: GOOGLE_AUTH_ENABLED=1 requires
GOOGLE_CLIENT_ID env`, the env var is empty — go back to step 1.2.
If step 4 returns `413 Request Entity Too Large`, your nginx body-size change
didn't apply — go back to step 2.

---

## 2. `GOOGLE_CLIENT_ID` — where it lives

The Web Client ID is set in **one place only**: `GOOGLE_CLIENT_ID` in
`/srv/proxy-infra/.env`. Both the FastAPI process and the static HTML pages
read it from there. The `.env.template` ships with the line empty and an
inline comment that points you at console.cloud.google.com.

```ini
# /srv/proxy-infra/.env
GOOGLE_AUTH_ENABLED=0          # ← flip to 1 to expose /api/auth/google
GOOGLE_CLIENT_ID=              # ← your …apps.googleusercontent.com string
```

### Setting it up on a fresh Google Cloud project

The OAuth client **must** be type "Web application" and have:

| Setting | Value |
| ------- | ----- |
| Application type | Web application |
| Authorized JavaScript origins | `https://litetube.trfnv.ru` (no path, no trailing slash) |
| Authorized redirect URIs | (none needed — GIS uses ID-token popup, not redirect) |

After creating, copy the **OAuth 2.0 Client ID** value (it ends in
`.apps.googleusercontent.com`) into `GOOGLE_CLIENT_ID`. Do not paste the
client secret — GIS client-side flow does not need it, and storing it in
`.env` would create a leak risk.

### Reading at runtime

`backend/api/litetube/auth.py`:

- `is_google_auth_enabled()` reads `GOOGLE_AUTH_ENABLED` **per request**,
  so flipping the env + restart exposes the endpoint without any code
  change. While `0`, `/api/auth/google` returns `404` before any
  google-auth library import — the dependency is lazy.
- `google_client_id()` reads `GOOGLE_CLIENT_ID` per request. Backed by an
  in-process fail-fast at import: if the flag is on but the id is empty,
  the API crashes at startup with a clear `RuntimeError`.

---

## 3. Hard gates BEFORE `GOOGLE_AUTH_ENABLED=1`

These are **mandatory** for production. Skipping any of them produces an
operator-visible outage when the flag goes live.

### 3.1 nginx `client_max_body_size 8k`

The `POST /api/auth/google` body is `{"id_token":"…"}`. A real Google ID
token is **≤ 2 KB** in practice, and the application caps it at **4096
chars** server-side (`auth.py:_GOOGLE_ID_TOKEN_MAX_LEN`). With the JSON
envelope around it, the worst-case body is ≈ 4.2 KB.

Starlette / FastAPI do **not** apply a default body-size cap; the
application-level cap simply rejects oversize tokens with `400
id_token_too_large`. **nginx**, however, default-rejects any body over
`1 MB` (and silently drops bodies between `client_max_body_size` and the
default `client_body_buffer_size`). Setting an explicit cap keeps small
legitimate requests flowing and rejects pathological bodies earlier.

```nginx
# /etc/nginx/sites-available/litetube.trfnv.ru.conf  (inside `server {}`)
client_max_body_size 8k;
```

Why **8 KB** and not 1 MB / 32 KB:

- A legitimate Google ID token is <2 KB; the JSON envelope pushes the
  worst case to ≈ 4.3 KB. 8 KB gives ~80% headroom for future fields
  (device_id, locale) without admitting obvious junk.
- Smaller cap reduces blast radius for any future endpoint-misuse bug.

After editing:

```bash
nginx -t                     # config-test
systemctl reload nginx
```

### 3.2 HTTPS-only

The Authorized JavaScript origin entry **must** be the HTTPS URL. If
you change the domain or move to an internal-only deployment, regenerate
the OAuth client. Mixing HTTPS-only attestation with an HTTP endpoint
silently fails the GIS init with no visible JS error — only the fallback
panel shows.

### 3.3 `JWT_SECRET` is present and ≥ 32 chars

Already enforced at import time by `auth.py`:

```
RuntimeError: Litube: JWT_SECRET env must be set to a string of >=32 chars.
```

If this fires, the operator bootstrap step (./scripts/init_operator.py)
was skipped or the `.env` lost its JWT_SECRET line.

### 3.4 Confirm with a fake-id smoke

Before going live with a real browser, test the failure-shapes:

```bash
curl -fsS -m 5 -X POST -H 'Content-Type: application/json' \
     -d '{"id_token":"x"}' \
     https://litetube.trfnv.ru/api/auth/google
#   expected: 401 {"detail":"invalid_google_token"}

# Build the 5000-char id_token + JSON envelope via a fixture file so quoting
# stays shell-portable (POSIX-baseline yes/head/tr pipeline — works under
# bash, dash, zsh, busybox ash rather than relying on bash brace expansion).
yes a | head -n 5000 | tr -d '\n' > /tmp/litetube-oversized-id-token.txt
{ printf '{"id_token":"'; cat /tmp/litetube-oversized-id-token.txt; printf '"}'; } \
    > /tmp/litetube-oversized-payload.json
curl -fsS -m 5 -X POST -H 'Content-Type: application/json' \
     --data-binary @/tmp/litetube-oversized-payload.json \
     https://litetube.trfnv.ru/api/auth/google
#   expected: 400 {"detail":"id_token_too_large"}
```

(`yes a | head -n 5000 | tr -d '\n'` is POSIX-portable — no brace expansion,
no nested quoting gymnastics.)

If either returns `413`, the nginx cap in step 3.1 is too small OR the cap
isn't applied (check with `nginx -T 2>/dev/null | grep client_max_body_size`).

---

## 4. Smoke-testing after the flag flip

```bash
# 1. /api/auth/google is now reachable (returns 401 with junk, not 404).
curl -fsS -m 5 -o /tmp/r.json -w '%{http_code}\n' \
     -X POST -H 'Content-Type: application/json' \
     -d '{"id_token":"x"}' \
     https://litetube.trfnv.ru/api/auth/google
#   expect: 401
cat /tmp/r.json | jq .
#   expect: {"detail":"invalid_google_token"}

# 2. Operator dashboard still works (regression check; should be 200).
curl -fsS -m 5 https://litetube.trfnv.ru/admin/ -o /dev/null -w '%{http_code}\n'
#   expect: 200

# 3. Email/password signup still works (regression check).
curl -fsS -m 5 -X POST -H 'Content-Type: application/json' \
     -d '{"email":"smoke@trfnv.ru","password":"correct-horse-battery-staple"}' \
     https://litetube.trfnv.ru/api/auth/signup
#   expect: 200 {"token":"…","user_id":N}
```

Then open `https://litetube.trfnv.ru/` in a browser and confirm:

- The `#g_id_signin` button is visible below the email/password form.
- Clicking it triggers the GIS account-picker popup.
- Selecting an account returns the user to `/?google_oauth=1` (or the
  `?code=…` URL flow on `activate.html`) with the cookie set.

If the button is missing or greyed out, check `/health` (see §9) and the
console (GIS init errors are silent in the DOM — see §6).

---

## 5. `POST /api/auth/google` failure modes

| HTTP | `detail`                    | Cause                                                              | Operator action                                                                                       |
| ---- | --------------------------- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| 400  | `id_token_required`         | Empty `id_token` field                                            | Verify GIS init succeeded; check browser console for `id-token-required` JS errors.                  |
| 400  | `id_token_too_large`        | Body > 4096-char id_token                                         | Recheck client (suspicious — real Google tokens are <2 KB).                                           |
| 401  | `invalid_google_token`      | `GoogleAuthError` or `ValueError` from verifier                    | Verify `GOOGLE_CLIENT_ID` matches the JS origin exactly (no trailing slash, no `https://` prefix).    |
| 403  | `unverified_email`          | Google-side `email_verified` claim missing/false                   | User must verify the email in their Google account before linking.                                   |
| 403  | `google_claims_incomplete`  | `sub` or `email` claim missing                                    | Bug in `verify_oauth2_token`; report with the request id.                                             |
| 403  | `admin_sso_disabled`        | Email collision with `role='operator'`                            | **Incident** — see §8.                                                                                |
| 409  | `google_sub_mismatch`       | Email already linked to a different Google account                 | User-side action only; they need to use the original Google account.                                  |
| 409  | `email_conflict`            | Race lost: a parallel request created the same email as a new user | User retries; idempotent.                                                                             |
| 503  | `google_unreachable`        | Network/DNS/timeout/RequestException from Google infra             | **Incident** — Google's certs/DNS/PKI are degraded or Russian network is blocking `googleapis.com`.   |

Anything not in the table above is a real `500` — escalate with the
request id (`cf-ray` header on Cloudflare, or `X-Request-Id` if the
reverse proxy emits one).

---

## 6. GIS-reachability fallback UX (accounts.google.com blocked)

### What the user sees

When `accounts.google.com/gsi/client` can't be fetched (CDN you're behind
is blocking it, or the user's network is), the GIS library never
initializes. Without our fallback, the user sees a flat `#g_id_signin`
div (no button) and gets no UX signal — they assume Litetube is broken.

With our fix (commit `b280af6`): the GIS `<script>` tag has an `onerror`
that reveals a hidden div:

```
Google Sign-In временно недоступен — используйте email и пароль
```

This sits underneath the (broken) GIS button slot. The email/password
form above remains fully functional — Google Sign-In is additive, never
required.

### Diagnostic steps for the operator

```bash
# 1. From the browser's network, users see either:
#    * `accounts.google.com/gsi/client` returns 200 but
#      `https://accounts.google.com/gsi/client` is blocked at the
#      corporate firewall / regional DPI level
#       → Operator can't fix that; user sees the fallback panel and
#         succeeds via email/password.
#
# 2. From the SERVER side:
curl -m 5 -fsS https://accounts.google.com/gsi/client -o /dev/null && echo OK
#   → Reports whether the SERVER can reach the GIS CDN. Note this does
#      NOT reflect what USERS see, since users' browsers fetch directly.

nslookup accounts.google.com 8.8.8.8
#   → Drone-free DNS resolution sanity.

# 3. From the SERVER, the /api/auth/google POST path tests a different
#    endpoint set (the verifier calls googleapis.com via
#    google.auth.transport.requests), but the same root cause manifests
#    in `503 google_unreachable` from §5.
```

### What you can do

- **You can't fix the user's local network block.** The fallback panel
  is the right tool: don't disable Google Sign-In over it.
- If YOUR server can't reach Google's endpoints either, the issue is
  ebgov-level DNS / IP-range blocking (Russia has had intermittent
  instances here). Sympathy, but no mitigation on Litetube side.
- If the fallback panel never reveals even when network is fine, the
  `onerror` handler isn't firing — meaning the GIS script loaded but
  didn't initialize. Check the browser console for the GIS error.

---

## 7. Emergency DB rollback — `downgrade_to_v5_to_v4`

**Use only when SCHEMA_V5 (`adce0f0`, makes `password_hash` nullable) has
been deployed to production and a critical regression requires restoring
the NOT NULL constraint. This is a last-resort data-preserving roll-down,
NOT a feature rollback.**

### Prerequisites — read these first

`downgrade_to_v5_to_v4` runs the inverse of `SCHEMA_V5` against
`/srv/proxy-infra/db/litetube.db`:

1. It creates `users_old`, copies `users` into it via explicit column
   list (matching V5 style), `DROP TABLE users`, `RENAME TO users`, and
   recreates the V4 partial unique index.
2. It deletes the `schema_version=5` row.
3. PRAGMA `foreign_keys` toggled OFF around the rebuild so child rows
   (proxies, bans, payments, device_claims) keep referencing users.

### Hard refusal conditions

The function **refuses** (raises `ValueError`/`RuntimeError`, no writes
happen) in either of these — read them carefully:

| Scenario                                                              | Function behaviour                                                  |
| --------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Schema not at v5 (`MAX(schema_version) != 5`)                         | `RuntimeError: downgrade_to_v5_to_v4: expected schema at v5, found vN; nothing to do.` |
| Any user has `password_hash IS NULL`                                  | `ValueError: downgrade_to_v5_to_v4: refused — N rows have NULL password_hash (Google-only users). Backfill or delete those rows before downgrading.` |

The second case is the common one: it means Google-only users
(created via `/api/auth/google` after the flag flip) have no password
hash to restore to NOT NULL.

### The rollout

```bash
# --- 0. STOP the API. Don't write to the DB while the downgrade runs. ---
cd /srv/proxy-infra && docker compose stop litetube-api

# --- 0.5. Force a WAL checkpoint before touching the file. ---
#      `docker compose stop` does NOT force a checkpoint — the -wal sidecar
#      may still hold unmerged writes, which can race with our rebuild below.
#      Truncate it now so the main DB file is canonical.
sqlite3 /srv/proxy-infra/db/litetube.db 'PRAGMA wal_checkpoint(TRUNCATE);'
# expect: 0 busy / 0 log / 0 checkpointed
ls -la /srv/proxy-infra/db/litetube.db*
# expect: litetube.db-wal is now 0 bytes (or absent); litetube.db-shm is 0 bytes (or absent).
# If the -wal file is still sizable, run `PRAGMA wal_checkpoint(TRUNCATE);` again.

# --- 1. Back up the DB (the function itself doesn't back up). ---
#      Capture a single timestamp so all four artifacts (main DB, -wal, -shm,
#      sqlite3 .backup) share the same prefix and clearly belong to one
#      coherent snapshot — three separate `$(date +…)` calls would desync.
SNAP=$(date +%Y%m%d%H%M%S)
cp /srv/proxy-infra/db/litetube.db     /srv/proxy-infra/db/litetube.db.pre-downgrade-$SNAP
cp /srv/proxy-infra/db/litetube.db-wal /srv/proxy-infra/db/litetube.db-wal.pre-downgrade-$SNAP 2>/dev/null || true
cp /srv/proxy-infra/db/litetube.db-shm /srv/proxy-infra/db/litetube.db-shm.pre-downgrade-$SNAP 2>/dev/null || true
sqlite3 /srv/proxy-infra/db/litetube.db ".backup /srv/proxy-infra/db/litetube.db.pre-downgrade-backup-$SNAP"

# --- 2. Inspect: are there password_hash=NULL rows? ---
sqlite3 /srv/proxy-infra/db/litetube.db \
        "SELECT id, email, role, status, google_sub FROM users WHERE password_hash IS NULL;"
# If this returns rows, you must decide for each:
#   * delete (next signup creates a fresh email-conflict risk)
#   * or backfill with a random bcrypt hash and an out-of-band note
#     saying "this user was a Google-only signup and lost SSO on
#     rollback; ask them to use email/password reset on first re-login".
# Recommended: dump them to a CSV with `sqlite3 ... .mode csv` and
# decide on a cut.

# --- 2.5 Sanity: confirm the working directory inside the container. ---
# The container's filesystem layout depends on docker-compose.yml. Confirm
# `/srv/proxy-infra/backend/api` is bind-mounted to the same path inside the
# FastAPI container before trusting the path below.
docker compose exec litetube-api pwd
docker compose exec litetube-api python3 -c 'import litetube.db; print(litetube.db.__file__)'
# expect: /srv/proxy-infra/backend/api/litetube/db.py (or wherever docker-compose.yml binds it)
# If your container maps it elsewhere, change the next step's `sys.path.insert(0, ...)`
# value to the in-container path that hosts `litetube/db.py`.

# --- 3. Run the downgrade ---
docker compose exec litetube-api python3 - <<'PY'
import sys
sys.path.insert(0, '/srv/proxy-infra/backend/api')
from litetube import db
db.downgrade_to_v5_to_v4('/srv/proxy-infra/db/litetube.db')
PY
# Expected last log line:
#   downgrade_to_v5_to_v4: rolled back to v4 — Google-only users (if any
#   were NULL before this call) are preserved; verify current
#   password_hash column is NOT NULL via `PRAGMA table_info(users)`.

# --- 4. Verify ---
sqlite3 /srv/proxy-infra/db/litetube.db "PRAGMA schema_version;"
# expect: 4
sqlite3 /srv/proxy-infra/db/litetube.db "PRAGMA table_info(users);" | grep -E 'password_hash'
# expect: password_hash|1|...|1   (the `notnull=1` indicates NOT NULL)

sqlite3 /srv/proxy-infra/db/litetube.db \
        "SELECT COUNT(*) FROM users WHERE password_hash IS NULL;"
# expect: 0  (every row has a postgres-hashed password)

# --- 5. Re-start the API ---
docker compose up -d --force-recreate litetube-api

# --- 6. Smoke ---
curl -fsS https://litetube.trfnv.ru/health
# expect: {"status":"ok","service":"litetube-api","version":"0.2.0"}
```

### Verification leftover

Run `python3 backend/api/litetube/db.py` is not a normal boot path; the
production roll-down is the function call above, not a normal app start.
The downgrade function uses synchronous `sqlite3.connect()` (not
aiosqlite) so it's safe to run from a one-shot shell.

### Recovery patterns

- **Downgrade succeeded, API is fine** — done. The user-visible behaviour
  is that Google-only users have lost SSO, but they can sign up fresh
  with email/password using the same email (operator-mediated).
- **Downgrade crashed mid-rebuild** — restore the pre-downgrade backup
  (`cp /srv/proxy-infra/db/litetube.db.pre-downgrade-* /srv/proxy-infra/db/litetube.db`).
  Resync the WAL files (`-wal`, `-shm`) from the same backup. Restart.
- **`ValueError: ... NULL password_hash`** — go back to step 2 and decide
  per-row. **Do NOT bypass the function** (e.g. by editing the SQL
  inline): the partial unique index will leave your DB in an unrunnable
  state if you `DROP TABLE users` without `INSERT`-ing all rows.

---

## 8. Operator-email collision incident

If `403 admin_sso_disabled` lands in your logs, an end-user is trying to
Google-Sign-In with an email that matches your operator account.

Action:

1. **Don't disable** Google Sign-In over a single collision. It's a
   safety feature working as intended (no admin via third-party SSO).
2. Audit `POST /api/auth/google` from the colliding IP for credential
   stuffing patterns. The operator account should only be logged into
   from the operator's actual IP range; if matches come from outside,
   treat as a brute-force precursor.
3. If it IS the operator themself confused about flows: walk them
   through using `/admin/` with email/password. Google SSO is for
   **clients only** by design.

---

## 9. Monitoring after the flip

```bash
# 1. /health reports build version, status, service name.
curl -fsS https://litetube.trfnv.ru/health | jq .
# expect:
#   {
#     "status": "ok",
#     "service": "litetube-api",
#     "version": "0.2.0"
#   }

# 2. /api/auth/google shape after the flip:
#     * without an Authorization header + with empty body → 401 invalid_google_token
#     * with a valid Google JWT → 200 + cookie set
#     * with a 5000-byte id_token → 400 id_token_too_large
# Track 401, 403, 409 as expected; alert on 503 rate.

# 3. Logs to tail for weekly rhythm:
journalctl -u litetube-api    | grep -E 'google_login:|google token verify failed|google auth infrastructure error'
# or, in container:
docker logs --since 24h litetube-litetube-api-1
```

Alert coverage after a successful flip:

- Elevated `503 google_unreachable` (>10/day) → likely reachability
  degradation; investigate GIS CDN state from the operator host.
- Elevated `400 id_token_too_large` (> 0/day, ever) → something is
  hitting the endpoint with a non-Google-shaped token. Suggests a
  misconfigured custom client or a brute-force pre-image.
- Any `500` in `POST /api/auth/google` → investigate immediately.

`alert_loops.py` (in-process) and `scripts/alert_daemon.py` (host-side)
catch nginx / uvicorn `5xx` events. Their cooldown state lives in the
SQLite `alert_state` table; flip them on by default.

---

## 10. Rollback levers — quick reference

| Rollback lever                       | Effort | Data loss                                   | When to use                                                                  |
| ------------------------------------ | ------ | ------------------------------------------- | ---------------------------------------------------------------------------- |
| `GOOGLE_AUTH_ENABLED=1 → 0`          | 5 s    | None                                        | Risk seen in production logs; path of least resistance.                      |
| `git revert adce0f0` (V5 → V4)       | 30 s   | None (V5 is purely additive)                | If you want the schema back to NOT NULL — but only use `downgrade_to_v5_to_v4`, not a git revert. |
| `downgrade_to_v5_to_v4(...)`         | Mid    | Google-only users (NULL `password_hash`)    | Last-resort schema roll-down (§7).                                            |
| `git revert b280af6` (GIS UI)        | 30 s   | None                                        | If the GIS UI breaks in unexpected ways for non-blocked users.               |
| `git revert 51e205d` (full Этап 1)    | 30 s   | None                                        | If Google Sign-In endpoint itself has a regression; safer than flag flip-off because all flows return to pre-Google-code state. |
| `git revert dd0bd8f` (v0.2.0 tag)    | 30 s   | None (only bumps version + CHANGELOG)      | Cosmetic; doesn't undo Этап 1/2/3 functionality.                              |

Always choose the **narrowest** lever that fixes the issue. The
`GOOGLE_AUTH_ENABLED` flip is the right answer for >95% of incidents.

---

## 11. Env-var quick reference

| Variable                | Default | Purpose                                                        |
| ----------------------- | ------- | -------------------------------------------------------------- |
| `GOOGLE_AUTH_ENABLED`   | `0`     | Master flag for `/api/auth/google`. Per-request read; restart required after flip. |
| `GOOGLE_CLIENT_ID`      | `""`    | OAuth 2.0 Web Client ID (`…apps.googleusercontent.com`).                                   |
| `JWT_SECRET`            | (none)  | HS256 signing key. **Required**, ≥ 32 chars.                          |
| `JWT_EXPIRY_HOURS`      | `24`    | TTL for client login cookies. Not used by Google Sign-In (uses 30-day fixed window). |
| `ALERT_COOLDOWN_SEC`    | `1800`  | Cross-process alert cooldown in SQLite `alert_state`.                    |
| `ALERT_PROXY_FAILURE_TICKS` | `10` | Proxy-failure ticks before an alert.                                       |

For the full list, see `backend/.env.template`.

---

## Appendix — file locations for the relevant code

| Concern                                | File                                                                         |
| -------------------------------------- | ---------------------------------------------------------------------------- |
| `is_google_auth_enabled()`             | `backend/api/litetube/auth.py` (top-level helper)                            |
| `google_login()` lookup/link/create    | `backend/api/litetube/auth.py` (function, line ~200+)                        |
| `_GOOGLE_ID_TOKEN_MAX_LEN = 4096` cap  | `backend/api/litetube/auth.py`                                              |
| `POST /api/auth/google` endpoint       | `backend/api/litetube/main.py`                                              |
| `_google_signin_block()` HTML          | `backend/api/litetube/main.py`                                              |
| Render cache key (flag + client_id)    | `backend/api/litetube/main.py:_RENDER_CACHE`                                 |
| GIS-onload + onerror fallback HTML     | `backend/api/litetube/main.py` (line ~110 of `_google_signin_block`)         |
| SCHEMA_V5 + downgrade_to_v5_to_v4      | `backend/api/litetube/db.py`                                                |
| `__version__` singleton                | `backend/api/litetube/__init__.py`                                          |
| Env template                           | `backend/.env.template`                                                     |
| nginx vhost                            | `nginx/nginx-default.conf` & `/etc/nginx/sites-available/litetube.trfnv.ru.conf` |
