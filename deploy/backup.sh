#!/bin/sh
# A consistent PostgreSQL snapshot; database password stays in container env.
set -eu
umask 077
cd "$(dirname "$0")/.."
mkdir -p backups
stamp="$(date -u +%Y%m%d-%H%M%S)"
target="backups/novel-$stamp.dump"
temporary="$target.partial"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
docker compose exec -T db pg_dump -U novel -d novel -Fc > "$temporary"
test -s "$temporary"
mv "$temporary" "$target"
printf '%s\n' "$target"
