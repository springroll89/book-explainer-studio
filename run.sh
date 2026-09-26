#!/bin/sh
set -eu
BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$BASE/.venv/bin/python" ]; then
  echo '请先运行：python3 -m venv .venv，再运行 .venv/bin/python -m pip install -r requirements.txt' >&2
  exit 1
fi
cd "$BASE"
exec "$BASE/.venv/bin/python" -m bookflow "$@"
