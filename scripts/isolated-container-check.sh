#!/usr/bin/env bash
# Run inside this project's Compose deployment only; never inspect or stop other services.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$project_dir"
compose_project="${AIRDROP_COMPOSE_PROJECT:-airdrop-v2-dev}"
docker compose -p "$compose_project" exec -T runner python -c "import os,socket; assert not os.path.exists('/run/secrets/master.key'); assert not os.path.exists('/app/data/airdrop.db'); assert os.getuid()!=0; s=socket.socket(); s.settimeout(1); result=s.connect_ex(('1.1.1.1',443)); assert result!=0; print('Runner: no master key, no database, non-root, outbound network blocked')"
docker compose -p "$compose_project" ps
