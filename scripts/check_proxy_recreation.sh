#!/usr/bin/env bash
# Development-only fault check: recreate this project's backend with a different IP.
# Named data volumes are retained; never stop or inspect unrelated services.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$project_dir"
compose() { docker compose -p airdrop-v2-dev -f docker-compose.yml -f deploy/compose.small-server.yml "$@"; }
compose exec -T backend python -c "from sqlalchemy import select,func; from backend.db import Session; from backend.models import Execution,Schedule; s=Session(); assert not s.scalar(select(func.count()).select_from(Execution).where(Execution.status.in_(['QUEUED','RUNNING']))); assert not s.scalar(select(func.count()).select_from(Schedule).where(Schedule.enabled==True)); s.close()"
backend_id="$(compose ps -q backend)"
frontend_id="$(compose ps -q frontend)"
old_ip="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$backend_id")"
[[ "$old_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]
reservation_id=''
cleanup() {
  if [[ -n "$reservation_id" ]]; then docker stop -t 1 "$reservation_id" >/dev/null || true; fi
}
trap cleanup EXIT
compose stop backend
compose rm -f backend
reservation_id="$(docker run -d --rm --name "airdrop-v2-dev-dns-reservation-$$" \
  --network airdrop-v2-dev_default --ip "$old_ip" --user 10001:10001 \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --memory 32m --cpus 0.1 --pids-limit 16 python:3.12-slim \
  python -c 'import time; time.sleep(180)')"
compose up --no-build -d --wait backend
new_ip="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(compose ps -q backend)")"
test "$new_ip" != "$old_ip"
test "$(compose ps -q frontend)" = "$frontend_id"
for attempt in $(seq 1 20); do
  if compose exec -T frontend wget -q -O /dev/null http://127.0.0.1:8080/api/health; then
    printf 'PASS frontend API reconnects after backend IP changes; frontend container unchanged\n'
    exit 0
  fi
  sleep 1
done
printf 'FAIL frontend did not reconnect to the recreated backend\n' >&2
exit 1
