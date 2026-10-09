#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$project_dir"
if [[ ! -e .venv ]]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python scripts/init_instance.py
docker compose -p airdrop-v2-dev up --build -d
printf 'AirDrop V2: http://127.0.0.1:18080\nLogin: %s/secrets/dev-login.txt\n' "$project_dir"
