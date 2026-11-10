#!/bin/bash
# Wait for harborstack to render var/ports.json, then start:
#   * a dind-side forwarder (shares the dind network namespace)
#   * a host-side loopback forwarder
set -euo pipefail

source "$(dirname "$0")/common.sh"

# This script must talk to the HOST Docker daemon, even if the caller exported
# DOCKER_HOST for harborstack.
unset DOCKER_HOST

DIND_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$DIND_NAME" 2>/dev/null || true)
[ -n "$DIND_IP" ] || die "cannot find dind container '$DIND_NAME'; run start-dind.sh first"

echo "$(date -u) dind_ip=$DIND_IP" >> "$LOG_DIR/forwarders.log"

# harborstack init writes var/ports.json after allocating ports.  The host
# forwarders must bind only after that point, otherwise probe.port_free()
# would see the ports as occupied and allocate a different set.
for _ in $(seq 1 2400); do
  [ -f "$PORTS_FILE" ] && break
  sleep 0.5
done
[ -f "$PORTS_FILE" ] || die "var/ports.json not found after 1200s"

PORTS=$("$REPO_ROOT/.venv/bin/python" - "$PORTS_FILE" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    data = json.load(f)
print(" ".join(str(v) for v in data.values()))
PY
)
[ -n "$PORTS" ] || die "var/ports.json is empty or invalid"
echo "$(date -u) ports=$PORTS" >> "$LOG_DIR/forwarders.log"

if ! docker image inspect "$DIND_PY_IMAGE" >/dev/null 2>&1; then
  echo "$(date -u) pulling $DIND_PY_IMAGE" >> "$LOG_DIR/forwarders.log"
  docker pull "$DIND_PY_IMAGE" >> "$LOG_DIR/forwarders.log" 2>&1 \
    || die "failed to pull $DIND_PY_IMAGE"
fi

# dind-side forwarder.  It shares the dind container's network namespace, so
# it can listen on the dind container IP and connect to the inner loopback.
docker rm -f "$DIND_SIDECAR_NAME" >/dev/null 2>&1 || true
docker run -d --name "$DIND_SIDECAR_NAME" \
  --network "container:$DIND_NAME" \
  --memory=128m --cpus=0.5 --restart=no \
  -v "$SCRIPT_DIR/forwarder.py:/forwarder.py:ro" \
  "$DIND_PY_IMAGE" \
  python /forwarder.py "$DIND_IP" 127.0.0.1 $PORTS >> "$LOG_DIR/forwarders.log" 2>&1

# host-side forwarder
: > "$PID_FILE"
nohup "$REPO_ROOT/.venv/bin/python" "$SCRIPT_DIR/forwarder.py" \
  127.0.0.1 "$DIND_IP" $PORTS >> "$LOG_DIR/forwarders.log" 2>&1 &
HPID=$!
echo "$HPID" > "$PID_FILE"
sleep 0.5
kill -0 "$HPID" 2>/dev/null || die "host forwarder exited; see $LOG_DIR/forwarders.log"
echo "$(date -u) host_forwarder_pid=$HPID" >> "$LOG_DIR/forwarders.log"
