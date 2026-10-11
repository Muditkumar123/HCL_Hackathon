#!/bin/sh
# First start only: build the DB from data/raw (generating the seeded pack if it is missing).
set -e
DB="${DB_PATH:-data/app.db}"
if [ "${SKIP_DATA_LOAD:-false}" != "true" ] && [ ! -f "$DB" ]; then
  if ! ls data/raw/*.csv >/dev/null 2>&1; then
    python -m scripts.generate_data --out data/raw
  fi
  python -m backend.loader --dir data/raw
fi
exec "$@"
