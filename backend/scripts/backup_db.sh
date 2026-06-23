#!/usr/bin/env bash
#
# Litetube — daily SQLite backup with 30-day rotation.
#
# Run from /etc/crontab:
#   0 3 * * * root /srv/proxy-infra/scripts/backup_db.sh
#
# Output: /srv/backups/proxy-infra/litetube-YYYY-MM-DD.db.gz (gzipped copy).
# Procedure:
#   1. Use `sqlite3 .backup` (snapshot mode — safe with WAL readers/writers).
#   2. gzip -9 the output file.
#   3. Run integrity_check on the uncompressed snapshot (catches corruption).
#   4. Log outcome and rotate anything older than 30 days.

set -uo pipefail

DB_SRC="${LITETUBE_DB_PATH:-/srv/proxy-infra/db/litetube.db}"
BACKUP_DIR="/srv/backups/proxy-infra"
RETENTION_DAYS="${LITETUBE_BACKUP_RETENTION_DAYS:-30}"

mkdir -p "$BACKUP_DIR"

if [[ ! -f "$DB_SRC" ]]; then
    echo "[$(date -Iseconds)] backup_db: no DB at $DB_SRC; skipping" \
        >> "$BACKUP_DIR/backup.log"
    exit 0
fi

date="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
tmp_snap="$(mktemp /tmp/litetube-XXXXXX.db)"
out="$BACKUP_DIR/litetube-${date}.db.gz"

if command -v sqlite3 >/dev/null 2>&1; then
    # Word-split the path into multiple args so sqlite3 receives a real filename,
    # not a literal '$tmp_snap'. Single-quoted earlier left the variable as text.
    sqlite3 "$DB_SRC" ".timeout 5000" ".backup $tmp_snap" 2>>"$BACKUP_DIR/backup.log"
else
    cp -a "$DB_SRC" "$tmp_snap"
fi

gzip -9 < "$tmp_snap" > "$out"
rm -f "$tmp_snap"

# Verify integrity. Unzip to a tempfile (sqlite3 :memory: does not consume
# stdin) and run integrity_check against it.
integrity_tmp="$(mktemp /tmp/litetube-integrity-XXXXXX.db)"
if ! zcat "$out" > "$integrity_tmp" 2>>"$BACKUP_DIR/backup.log"; then
    echo "[$(date -Iseconds)] backup_db: gunzip failed for $out" >> "$BACKUP_DIR/backup.log"
    rm -f "$integrity_tmp"
    exit 1
fi
if sqlite3 "$integrity_tmp" "PRAGMA integrity_check;" 2>/dev/null | tail -1 | grep -q '^ok$'; then
    echo "[$(date -Iseconds)] backup_db: ok $out  ($(stat -c%s "$out") bytes)" \
        >> "$BACKUP_DIR/backup.log"
    rm -f "$integrity_tmp"
else
    echo "[$(date -Iseconds)] backup_db: INTEGRITY CHECK FAILED -- $out retained" \
        >> "$BACKUP_DIR/backup.log"
    rm -f "$integrity_tmp"
    exit 1
fi

# Rotate old backups
find "$BACKUP_DIR" -maxdepth 1 -name "litetube-*.db.gz" -mtime "+${RETENTION_DAYS}" -delete \
    && echo "[$(date -Iseconds)] backup_db: rotated older than ${RETENTION_DAYS}d" \
       >> "$BACKUP_DIR/backup.log"
