#!/usr/bin/env bash
#
# Litetube — one-shot setup from a fresh clone at /srv/proxy-infra/.
#
# Run as root (or sudo). Idempotent -- safe to rerun. Sequence:
#   1. pre-flight: dependencies, .env presence
#   2. install ssl-cert (Debian snakeoil cert package) -- required so our
#      vhost HTTPS blocks (which start with snakeoil paths) successfully
#      load before Let's Encrypt certs are issued
#   3. validate source files exist before symlinking
#   4. symlink nginx vhosts into sites-enabled + conf.d
#   5. drop Debian default 'Welcome to nginx' (replaced by our default_server)
#   6. nginx -t && systemctl reload nginx (snakeoil HTTPS loads cleanly)
#   7. issue Let's Encrypt certs via webroot HTTP-01 challenge
#      (our vhost already serves /.well-known/acme-challenge/)
#   8. sed-replace ssl_certificate* paths in our vhost files from snakeoil
#      to the freshly-issued LE paths
#   9. nginx -t && systemctl reload nginx (HTTPS now uses LE)
#  10. docker compose build + up -d
#  11. init operator account (random password printed once)
#  12. install daily backup cron
#  13. install litetube-alerter systemd unit (host-side log-watcher)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

need() { command -v "$1" >/dev/null 2>&1 || { echo "[setup.sh] missing command: $1"; exit 2; }; }
need docker
need nginx
need sqlite3

# 1. pre-flight
[[ -f "$ROOT/.env" ]] || { echo "[setup.sh] $ROOT/.env not found; copy from .env.template first"; exit 1; }
mkdir -p /srv/proxy-infra/db \
         /srv/proxy-infra/3proxy-data \
         /srv/proxy-infra/3proxy-log \
         /srv/proxy-infra/logs \
         /var/www/litetube-acme
chown -R www-data:www-data /var/www/litetube-acme

# 2. install ssl-cert -- provides snakeoil cert files our vhosts reference
#    initially. Without this, nginx -t fails on first deploy.
if ! dpkg -l ssl-cert 2>/dev/null | grep -q '^ii'; then
    echo "[setup.sh] installing ssl-cert (Debian snakeoil cert package)"
    apt-get update -qq
    apt-get install -y ssl-cert
fi
[[ -f /etc/ssl/certs/ssl-cert-snakeoil.pem ]] || { echo "[setup.sh] /etc/ssl/certs/ssl-cert-snakeoil.pem missing after install"; exit 1; }
[[ -f /etc/ssl/private/ssl-cert-snakeoil.key ]] || { echo "[setup.sh] /etc/ssl/private/ssl-cert-snakeoil.key missing after install"; exit 1; }

# Pre-flight certbot
if ! command -v certbot >/dev/null 2>&1; then
    echo "[setup.sh] installing certbot + python3-certbot-nginx"
    apt-get install -y certbot python3-certbot-nginx
fi

# 3. validate source files exist before symlinking (catch typos early)
for f in \
    "$ROOT/nginx/litetube.trfnv.ru.conf" \
    "$ROOT/nginx/admin.litetube.trfnv.ru.conf" \
    "$ROOT/nginx/api.litetube.trfnv.ru.conf" \
    "$ROOT/nginx/conf.d/litetube-rate-limits.conf" \
    "$ROOT/nginx/conf.d/litetube-default-server.conf" \
    "$ROOT/docker-compose.yml" \
    "$ROOT/.env"; do
    [[ -f "$f" ]] || { echo "[setup.sh] required file missing: $f"; exit 1; }
done

# 4. symlinks (force so re-run refreshes)
ln -sf "$ROOT/nginx/litetube.trfnv.ru.conf" /etc/nginx/sites-enabled/litetube.trfnv.ru.conf
ln -sf "$ROOT/nginx/admin.litetube.trfnv.ru.conf" /etc/nginx/sites-enabled/admin.litetube.trfnv.ru.conf
ln -sf "$ROOT/nginx/api.litetube.trfnv.ru.conf"   /etc/nginx/sites-enabled/api.litetube.trfnv.ru.conf
ln -sf "$ROOT/nginx/conf.d/litetube-rate-limits.conf"    /etc/nginx/conf.d/litetube-rate-limits.conf
ln -sf "$ROOT/nginx/conf.d/litetube-default-server.conf" /etc/nginx/conf.d/litetube-default-server.conf

# 5. drop Debian default Welcome-to-nginx -- we ship our own default_server catch-all.
rm -f /etc/nginx/sites-enabled/default

# Ensure /etc/letsencrypt and essential SSL config files exist before reloading Nginx.
mkdir -p /etc/letsencrypt
if [[ ! -f /etc/letsencrypt/ssl-dhparams.pem ]]; then
    echo "[setup.sh] generating ssl-dhparams.pem (this may take a moment)"
    openssl dhparam -out /etc/letsencrypt/ssl-dhparams.pem 2048
fi
if [[ ! -f /etc/letsencrypt/options-ssl-nginx.conf ]]; then
    echo "[setup.sh] creating options-ssl-nginx.conf placeholder"
    cat > /etc/letsencrypt/options-ssl-nginx.conf <<'EOF'
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_ciphers "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384";
EOF
fi

# 6. nginx -t && reload -- snakeoil placeholder paths now exist on disk.
nginx -t && systemctl reload nginx

# 7. issue Let's Encrypt certs (idempotent). Uses --webroot mode because the
#    nginx plugin would try to read running nginx config which may not yet
#    include the freshly symlinked vhosts.
bash "$ROOT/scripts/certbot_run.sh"

# 8. sed-replace ssl_certificate* paths in our vhosts from snakeoil -> LE.
le_domains=(litetube.trfnv.ru admin.litetube.trfnv.ru api.litetube.trfnv.ru)
for d in "${le_domains[@]}"; do
    vhost="/etc/nginx/sites-enabled/${d}.conf"
    [[ -f "/etc/letsencrypt/live/${d}/fullchain.pem" ]] || { echo "[setup.sh] missing cert for $d, certbot_run.sh must have failed"; exit 1; }
    sed -i \
        -e "s|/etc/ssl/certs/ssl-cert-snakeoil.pem|/etc/letsencrypt/live/${d}/fullchain.pem|" \
        -e "s|/etc/ssl/private/ssl-cert-snakeoil.key|/etc/letsencrypt/live/${d}/privkey.pem|" \
        "$vhost"
done

# 9. nginx -t && reload -- now using LE certs.
nginx -t && systemctl reload nginx

# 10. docker compose
docker compose -f "$ROOT/docker-compose.yml" build
docker compose -f "$ROOT/docker-compose.yml" up -d

# 11. init operator account (random password printed once)
OPERATOR_EMAIL="$(grep -E '^OPERATOR_EMAIL=' "$ROOT/.env" | cut -d= -f2- | tr -d '\r' || true)"
if [[ -z "${OPERATOR_EMAIL:-}" ]]; then
    OPERATOR_EMAIL="admin@litetube.trfnv.ru"
fi
docker compose -f "$ROOT/docker-compose.yml" exec -T fastapi \
    python3 /app/scripts/init_operator.py \
    --email "$OPERATOR_EMAIL" \
    --random-password

# 12. backup cron (daily 03:00 UTC)
if ! grep -q '/srv/proxy-infra/scripts/backup_db.sh' /etc/crontab 2>/dev/null; then
    printf '0 3 * * * root /srv/proxy-infra/scripts/backup_db.sh\n' >> /etc/crontab
    echo "[setup.sh] backup cron installed in /etc/crontab"
fi

# 13. install litetube-alerter systemd unit (host-side log-watcher).
#     Tails nginx + uvicorn access logs; emits admin-5xx alerts with
#     cooldown-shared state in /srv/proxy-infra/db/litetube.db. Idempotent.
mkdir -p /var/log/litetube
chmod 0755 /var/log/litetube || true
if [[ -f "$ROOT/scripts/litetube-alerter.service" ]]; then
    install -m 0644 "$ROOT/scripts/litetube-alerter.service" \
        /etc/systemd/system/litetube-alerter.service
    systemctl daemon-reload
    if ! systemctl is-enabled --quiet litetube-alerter.service; then
        systemctl enable litetube-alerter.service
    fi
    systemctl restart litetube-alerter.service
    echo "[setup.sh] litetube-alerter.service installed and restarted"
fi

cat <<EOF
=== setup.sh complete ===
DOMAIN:       https://litetube.trfnv.ru/
ADMIN UI:     https://admin.litetube.trfnv.ru/
API:          https://api.litetube.trfnv.ru/
PROXY SOCKS5: litetube.trfnv.ru:11001
PROXY HTTP:   litetube.trfnv.ru:11002

OPERATOR PASSWORD printed above — STORE IT.
- Tail app logs:
    cd $ROOT && docker compose logs -f fastapi
    cd $ROOT && docker compose logs -f 3proxy
EOF
