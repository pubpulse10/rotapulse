#!/bin/sh
# RotaPulse container entrypoint.
#
# 1. If the persistent disk is empty (fresh disk, or disaster recovery), pull
#    the latest snapshot from R2 BEFORE the app boots. -if-db-not-exists means
#    we never clobber a good local DB; -if-replica-exists means the very first
#    deploy (no backup yet) is not treated as an error.
# 2. Hand off to Litestream, which runs the web server as a child process and
#    streams every WAL change up to R2 for as long as the app is alive.
set -e

DB_PATH=/app/data/rotapulse.db

echo "[entrypoint] Restoring $DB_PATH from replica if needed..."
litestream restore -if-db-not-exists -if-replica-exists -config /etc/litestream.yml "$DB_PATH"

echo "[entrypoint] Starting Litestream replication + web server..."
exec litestream replicate -config /etc/litestream.yml \
  -exec "waitress-serve --host=0.0.0.0 --port=5053 wsgi:app"
