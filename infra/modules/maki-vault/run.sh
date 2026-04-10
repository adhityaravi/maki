#!/bin/bash
set -e
DB="${1:-maki}"
MIGRATIONS_DIR="$(dirname "$0")"

psql -U maki -d "$DB" -c "
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);"

for f in "$MIGRATIONS_DIR"/[0-9]*.sql; do
    [ -f "$f" ] || continue
    version="$(basename "$f")"
    applied=$(psql -U maki -d "$DB" -tAc \
        "SELECT 1 FROM schema_migrations WHERE version = '$version';")
    if [ "$applied" = "1" ]; then
        echo "SKIP $version (already applied)"
        continue
    fi
    echo "APPLY $version"
    psql -U maki -d "$DB" --single-transaction -f "$f"
    psql -U maki -d "$DB" -c \
        "INSERT INTO schema_migrations (version) VALUES ('$version');"
done

echo "Migrations complete."
