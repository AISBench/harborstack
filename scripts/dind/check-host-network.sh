#!/bin/bash
# Check whether the host Docker daemon allows container-to-container traffic.
#
# Exit 0: host bridge works -> the standard host-Docker path is available.
# Exit 1: host bridge is blocked -> use the dind path.
set -uo pipefail

source "$(dirname "$0")/common.sh"
unset DOCKER_HOST

IMG="$MIRROR/library/redis:7-alpine"
NET="harborstack-netcheck-$$"
A="harborstack-netcheck-a-$$"
B="harborstack-netcheck-b-$$"

cleanup() {
  docker rm -f "$A" "$B" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "==> pulling $IMG"
  docker pull "$IMG" >/dev/null 2>&1 || {
    echo "FAIL: cannot pull $IMG" >&2
    exit 1
  }
fi

echo "==> creating temporary network $NET"
docker network create "$NET" >/dev/null 2>&1 || {
  echo "FAIL: cannot create temporary network" >&2
  exit 1
}
docker run -d --name "$A" --network "$NET" "$IMG" >/dev/null 2>&1
docker run -d --name "$B" --network "$NET" "$IMG" >/dev/null 2>&1
sleep 2

BIP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$B" 2>/dev/null)
if [ -z "$BIP" ]; then
  echo "FAIL: cannot determine temporary container IP" >&2
  exit 1
fi

OUT=$(docker exec "$A" redis-cli -h "$BIP" -p 6379 ping 2>&1)
echo "container A -> B: $OUT"
if [ "$OUT" = "PONG" ]; then
  echo "RESULT: host bridge OK; the standard host-Docker path is available."
  exit 0
fi
echo "RESULT: host bridge BLOCKED; use the dind path."
exit 1
