#!/bin/sh
set -e

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

if [ ! -f "$DATA_DIR/lsj.sqlite" ]; then
    echo "==> No LSJ index found at $DATA_DIR/lsj.sqlite. Building (one-time, ~5-10 min)..."
    python scripts/build_lsj.py --clean
    echo "==> LSJ index built."
else
    echo "==> Using existing LSJ index at $DATA_DIR/lsj.sqlite"
fi

exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --workers 2 --timeout 120 app:app
