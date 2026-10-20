#!/bin/sh
# Back up WitX (Compose deployment): the database, the screenshots, and the plugins with
# the skills written in WitX (D89), which exist nowhere else.
#
#   deploy/backup.sh            # writes deploy/backups/witx-<time>.dump, objects-<time>.tar.gz
#                               # and plugins-<time>.tar.gz
#
# The restore ledger (objects under ledger/) is left out on purpose: it records what was
# deleted or revoked after a backup, so a restore must never take it back in time.
# Backups older than WITX_BACKUP_KEEP_DAYS (default 30, spec §19) are removed: deleted
# content lingers in backups at most that long.
set -eu
cd "$(dirname "$0")"
dest=${WITX_BACKUP_DIR:-./backups}
keep=${WITX_BACKUP_KEEP_DAYS:-30}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$dest"
docker compose exec -T postgres pg_dump -U postgres -d witx -Fc >"$dest/witx-$stamp.dump"
docker compose run --rm --no-deps -T --entrypoint tar api \
  -C /data/objects --exclude=./ledger -cz . >"$dest/objects-$stamp.tar.gz"
docker compose run --rm --no-deps -T --entrypoint tar api \
  -C /data/plugins -cz . >"$dest/plugins-$stamp.tar.gz"
find "$dest" \( -name 'witx-*.dump' -o -name 'objects-*.tar.gz' -o -name 'plugins-*.tar.gz' \) -mtime +"$keep" -delete
echo "backed up as $stamp in $dest"
