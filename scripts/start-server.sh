#!/usr/bin/env bash
# Build serially with limits; initialize only this directory and its Compose volumes.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$project_dir"
build_memory="${AIRDROP_BUILD_MEMORY:-384m}"
build_cpu_quota="${AIRDROP_BUILD_CPU_QUOTA:-50000}"
# Legacy builder supports build-time memory/CPU limits without changing daemon settings.
export DOCKER_BUILDKIT=0
docker build --memory="$build_memory" --cpu-quota="$build_cpu_quota" \
  --build-arg="PYPI_INDEX=${AIRDROP_PYPI_INDEX:-https://pypi.org/simple}" \
  -t airdrop-v2-dev-backend:local -f deploy/backend.Dockerfile .
docker run --rm --network none --memory=128m --cpus=0.25 \
  --user="$(id -u):$(id -g)" --volume="$project_dir:/app" \
  airdrop-v2-dev-backend:local python scripts/init_instance.py
docker build --memory="$build_memory" --cpu-quota="$build_cpu_quota" \
  --build-arg="PYPI_INDEX=${AIRDROP_PYPI_INDEX:-https://pypi.org/simple}" \
  -t airdrop-v2-dev-runner:local -f deploy/runner.Dockerfile .
docker build --memory="$build_memory" --cpu-quota="$build_cpu_quota" \
  --build-arg="NODE_BASE=${AIRDROP_NODE_BASE:-node:22-alpine}" \
  --build-arg="NGINX_BASE=${AIRDROP_NGINX_BASE:-nginxinc/nginx-unprivileged:alpine}" \
  -t airdrop-v2-dev-frontend:local -f deploy/frontend.Dockerfile .
docker compose -p airdrop-v2-dev -f docker-compose.yml \
  -f deploy/compose.small-server.yml up --no-build -d --wait
printf 'AirDrop V2 started. Login file: %s/secrets/dev-login.txt\n' "$project_dir"
