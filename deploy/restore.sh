#!/bin/sh
# Restore WitX (Compose deployment) from a backup made by backup.sh.
#
#   deploy/restore.sh 20261012T080000Z
#
# Stops the API, workers and web server, restores the database, the screenshots and the
# plugins (when the backup has them: older ones do not), then
# runs `witx admin recover` before anything starts again: it replays the restore ledger
# (tasks deleted and Gateways or devices disabled after the backup stay so) and stops
# what was in flight (spec §16.2). Leaves ledger/ in the object store untouched.
set -eu
cd "$(dirname "$0")"
stamp=$1
src=${WITX_BACKUP_DIR:-./backups}
test -f "$src/witx-$stamp.dump" && test -f "$src/objects-$stamp.tar.gz"
docker compose stop web api worker
docker compose exec -T postgres pg_restore -U postgres -d witx --clean --if-exists \
  --single-transaction <"$src/witx-$stamp.dump"
docker compose run --rm --no-deps -T --entrypoint tar api \
  -C /data/objects --exclude=./ledger -xz <"$src/objects-$stamp.tar.gz"
if test -f "$src/plugins-$stamp.tar.gz"; then
  docker compose run --rm --no-deps -T --entrypoint sh api \
    -c 'find /data/plugins -mindepth 1 -delete && tar -C /data/plugins -xz' \
    <"$src/plugins-$stamp.tar.gz"
fi
docker compose run --rm api admin recover
docker compose up -d
echo "restored $stamp; owners re-enroll Gateways whose credential changed since, and"
echo "settle unknown inputs from new screenshots before their devices take tasks"
