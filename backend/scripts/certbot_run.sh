#!/usr/bin/env bash
#
# Litetube — issue Let's Encrypt certificates for the three subdomains.
#
# Run AFTER nginx vhost symlinks are in /etc/nginx/sites-enabled/ AND nginx
# has been reloaded (because certbot --nginx reads server_name from running
# config). Idempotent: re-running issues nothing for already-existing certs.
#
# Outputs certs to /etc/letsencrypt/live/<domain>/ as expected by vhost
# ssl_certificate/ ssl_certificate_key directives.

set -uo pipefail

domains=(
    "litetube.trfnv.ru"
    "admin.litetube.trfnv.ru"
    "api.litetube.trfnv.ru"
)

acme_root="/var/www/litetube-acme"
mkdir -p "$acme_root"
chown -R www-data:www-data "$acme_root" 2>/dev/null || \
    echo "[certbot_run] chown www-data failed; continuing" >&2

if ! command -v certbot >/dev/null 2>&1; then
    echo "[certbot_run] certbot not found; install via: apt-get install -y certbot python3-certbot-nginx"
    exit 2
fi

# Uses --webroot so we don't need certbot's nginx plugin to read running
# nginx config (which may not yet include our newly-symlinked vhosts). Our
# vhost already serves /.well-known/acme-challenge/ from $acme_root.
for d in "${domains[@]}"; do
    if [[ -f "/etc/letsencrypt/live/$d/fullchain.pem" ]]; then
        echo "[certbot_run] $d: cert present; skipping issue"
        continue
    fi
    echo "[certbot_run] $d: issuing"
    certbot certonly \
        --webroot \
        -w "$acme_root" \
        --non-interactive \
        --agree-tos \
        --email "ops@litetube.trfnv.ru" \
        --no-eff-email \
        --cert-name "$d" \
        -d "$d" \
        --key-type ecdsa \
        --elliptic-curve secp384r1
done

# Ensure the dhparams file exists. certbot 2.x generates it on first issue;
# if no certs were just issued and /etc/letsencrypt/ssl-dhparams.pem is missing,
# regenerate it.
if [[ ! -f /etc/letsencrypt/ssl-dhparams.pem ]]; then
    echo "[certbot_run] generating ssl-dhparams.pem (this may take a moment)"
    openssl dhparam -out /etc/letsencrypt/ssl-dhparams.pem 2048
fi

echo "[certbot_run] done."
