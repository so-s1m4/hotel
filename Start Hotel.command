#!/bin/zsh
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print 'Create the Python environment first: python3 -m venv .venv && .venv/bin/pip install -e .'
  exit 1
fi
if [[ ! -f hotel_web/dist/index.html ]]; then
  (cd hotel_web && npm ci --no-audit --no-fund && npm run build)
fi
exec .venv/bin/python -m hotel_bot
