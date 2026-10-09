#!/usr/bin/env bash
# Uses only this project's files, containers and one dedicated HTTPS port.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$project_dir"
public_ip="${AIRDROP_PUBLIC_IP:-47.98.183.77}"
public_port="${AIRDROP_PUBLIC_PORT:-18443}"
python3 -c 'import ipaddress,sys; assert ipaddress.ip_address(sys.argv[1]).version==4; assert 1024<int(sys.argv[2])<65536' "$public_ip" "$public_port"
export AIRDROP_PUBLIC_ORIGIN="https://$public_ip:$public_port"
export AIRDROP_PUBLIC_PORT="$public_port"
certificate_dir="$project_dir/secrets/public-web"
if [[ ! -e "$certificate_dir/server.key" && ! -e "$certificate_dir/server.crt" ]]; then
    install -d -m 700 "$certificate_dir"
    umask 077
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
      -days 365 -subj "/CN=$public_ip" \
      -addext "subjectAltName=IP:$public_ip,IP:127.0.0.1" \
      -addext 'basicConstraints=critical,CA:FALSE' \
      -addext 'keyUsage=critical,digitalSignature' \
      -addext 'extendedKeyUsage=serverAuth' \
      -keyout "$certificate_dir/server.key" -out "$certificate_dir/server.crt"
fi
test -f "$certificate_dir/server.crt" && test -f "$certificate_dir/server.key"
openssl x509 -in "$certificate_dir/server.crt" -checkend 2592000 -noout
openssl x509 -in "$certificate_dir/server.crt" -checkip "$public_ip" -noout
chown root:101 "$certificate_dir/server.key"
chmod 640 "$certificate_dir/server.key"
chmod 644 "$certificate_dir/server.crt"
python3 - "$AIRDROP_PUBLIC_ORIGIN" "$AIRDROP_PUBLIC_PORT" <<'PY'
import os, sys, tempfile
from pathlib import Path
path = Path('.env')
values = {'AIRDROP_PUBLIC_ORIGIN': sys.argv[1], 'AIRDROP_PUBLIC_PORT': sys.argv[2],
          'AIRDROP_SECURE_COOKIE': 'true', 'AIRDROP_ALLOWED_ORIGINS': sys.argv[1]}
lines = [line for line in path.read_text().splitlines() if line.split('=', 1)[0] not in values]
fd, name = tempfile.mkstemp(prefix='.public-web-env-', dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write('\n'.join(lines + [key + '=' + value for key, value in values.items()]) + '\n')
    os.replace(name, path)
finally:
    Path(name).unlink(missing_ok=True)
PY
compose=(docker compose -p airdrop-v2-dev -f docker-compose.yml -f deploy/compose.small-server.yml -f deploy/compose.public-ip.yml)
"${compose[@]}" config --quiet
"${compose[@]}" run --rm --no-deps public-web -t
"${compose[@]}" up -d --no-build --no-deps --wait --wait-timeout 90 backend public-web
curl --fail --silent --cacert "$certificate_dir/server.crt" "https://127.0.0.1:$public_port/api/health"
printf '\nPublic URL: %s\n' "$AIRDROP_PUBLIC_ORIGIN"
openssl x509 -in "$certificate_dir/server.crt" -noout -fingerprint -sha256 -enddate
