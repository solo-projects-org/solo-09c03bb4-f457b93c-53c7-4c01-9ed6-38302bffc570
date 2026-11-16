#!/bin/sh
# Fail fast if the shared data directory is not writable, which is the
# usual cause of a broken bind-mount.
set -e

DATA_DIR="$(dirname "${FOREST_DB_PATH:-/data/forest.db}")"
mkdir -p "$DATA_DIR"
if [ ! -w "$DATA_DIR" ]; then
  echo "ERROR: $DATA_DIR is not writable by uid $(id -u)." >&2
  echo "Fix ownership of the host directory, e.g. chown -R 1000:1000 ./data" >&2
  exit 1
fi

exec "$@"
