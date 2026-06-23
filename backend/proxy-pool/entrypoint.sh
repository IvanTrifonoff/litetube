#!/usr/bin/env bash
#
# Litube 3proxy container entrypoint.
#
# Generates a per-container-start random placeholder password and replaces
# the literal in /var/lib/3proxy/3proxy.cfg BEFORE supervisord starts 3proxy.
# This ensures the literal in git ('_bootstrap_locked_dummy_') is never
# actually present in the running container — the placeholder that any
# 'nobody'-credential observer could extract from our public repo cannot
# be used to authenticate against a live deployment.
#
# The FastAPI reload_worker mirrors this on every SIGHUP: when the user list
# is empty (no real users), it writes `users nobody:CL:<random_hex>` with
# a fresh random token. Same security property holds post-reload.

set -uo pipefail

CFG="/var/lib/3proxy/3proxy.cfg"
BOOTSTRAP_CFG="/etc/3proxy.cfg.bootstrap"

if [[ ! -f "$CFG" ]]; then
    if [[ -f "$BOOTSTRAP_CFG" ]]; then
        echo "[entrypoint] $CFG missing; copying bootstrap config"
        cp "$BOOTSTRAP_CFG" "$CFG"
    else
        echo "[entrypoint] FATAL: bootstrap cfg $BOOTSTRAP_CFG missing"
        exit 1
    fi
fi

# Generate a 32-hex-char password from /dev/urandom (no openssl dep on
# the Alpine base image; busybox doesn't ship openssl).
DUMMY_PW="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
echo "[entrypoint] generated placeholder password (32 hex chars)"

# Replace literal if it still exists. Use a Perl-based or standard sed replacement.
if grep -q "_bootstrap_locked_dummy_" "$CFG" 2>/dev/null; then
    echo "[entrypoint] replacing bootstrap placeholder password in $CFG"
    sed -i "s/_bootstrap_locked_dummy_/${DUMMY_PW}/" "$CFG"
fi
chmod 0644 "$CFG"
chown litetube:litetube "$CFG"

echo "[entrypoint] starting supervisord (3proxy parent will load cfg)"
exec supervisord --configuration=/etc/supervisord.conf --nodaemon
