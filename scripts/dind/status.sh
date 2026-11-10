#!/bin/bash
# One-shot status for the dind path.
set -u

source "$(dirname "$0")/common.sh"
unset DOCKER_HOST

echo "== config"
echo "  DIND_NAME=$DIND_NAME"
echo "  DIND_VOLUME=$DIND_VOLUME"
echo "  DOCKER_HOST=$DOCKER_HOST_DIND"
echo "  MIRROR=$MIRROR"

echo
echo "== host containers"
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null \
  | grep -E "^(${DIND_NAME}|${DIND_SIDECAR_NAME})\b" || echo "  (no dind/sidecar container)"

echo
echo "== dind daemon"
if DOCKER_HOST="$DOCKER_HOST_DIND" docker info >/dev/null 2>&1; then
  DOCKER_HOST="$DOCKER_HOST_DIND" docker version \
    --format '  server={{.Server.Version}} API={{.Server.APIVersion}}'
  echo "  inner containers:"
  DOCKER_HOST="$DOCKER_HOST_DIND" docker ps \
    --format '  {{.Names}}\t{{.Status}}' 2>/dev/null || true
else
  echo "  (dind daemon not reachable)"
fi

echo
echo "== host forwarders"
if [ -f "$PID_FILE" ]; then
  while read -r pid; do
    [ -n "$pid" ] || continue
    if kill -0 "$pid" 2>/dev/null; then
      echo "  pid $pid running"
    else
      echo "  pid $pid not running (stale pid file)"
    fi
  done < "$PID_FILE"
else
  echo "  (no host-forwarders.pids)"
fi

echo
echo "== loopback ports"
ss -tln 2>/dev/null | awk '$4 ~ /:189[0-9][0-9]$/ {print "  " $4}' || true
[ -n "$(ss -tln 2>/dev/null | awk '$4 ~ /:189[0-9][0-9]$/')" ] || echo "  (none)"

echo
echo "== web health"
if [ -f "$PORTS_FILE" ]; then
  WEB=$("$REPO_ROOT/.venv/bin/python" - "$PORTS_FILE" <<'PY' 2>/dev/null || true
import json
import sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("web", ""))
PY
)
  if [ -n "$WEB" ]; then
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
      "http://127.0.0.1:$WEB/api/public/health" 2>/dev/null || true)
    echo "  http://127.0.0.1:$WEB/api/public/health -> ${code:-unreachable}"
  else
    echo "  (no web port in var/ports.json)"
  fi
else
  echo "  (var/ports.json not found; run scripts/dind/run-init.sh)"
fi
