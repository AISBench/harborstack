#!/bin/bash
# Stop the dind path.  By default the dind container is stopped and kept,
# and the named volume (Langfuse data) is preserved.
#
# Usage:
#   stop-dind.sh            # stop container, keep container + volume
#   stop-dind.sh --purge    # remove container and volume (DATA LOSS)
set -uo pipefail

source "$(dirname "$0")/common.sh"
unset DOCKER_HOST

if [ -f "$WATCHER_PID_FILE" ]; then
  kill "$(cat "$WATCHER_PID_FILE")" 2>/dev/null || true
  rm -f "$WATCHER_PID_FILE"
fi
if [ -f "$PID_FILE" ]; then
  while read -r p; do
    [ -n "$p" ] && kill "$p" 2>/dev/null || true
  done < "$PID_FILE"
  rm -f "$PID_FILE"
fi
docker rm -f "$DIND_SIDECAR_NAME" >/dev/null 2>&1 || true

if [ "${1:-}" = "--purge" ]; then
  echo "==> removing dind container and volume (data will be lost)"
  docker rm -f "$DIND_NAME" >/dev/null 2>&1 || true
  docker volume rm "$DIND_VOLUME" >/dev/null 2>&1 || true
else
  echo "==> stopping dind container (volume preserved)"
  docker stop "$DIND_NAME" >/dev/null 2>&1 || true
fi

echo "done"
