#!/bin/bash
# Run harborstack init against the dind daemon, with the port forwarders
# started automatically as soon as var/ports.json is rendered.
set -uo pipefail

source "$(dirname "$0")/common.sh"

# Host Docker is used to clean up old forwarders / sidecar.
unset DOCKER_HOST

[ -x "$REPO_ROOT/.venv/bin/harborstack" ] \
  || die "missing $REPO_ROOT/.venv/bin/harborstack; run 'uv sync' first"
[ -x "$REPO_ROOT/.venv/bin/python" ] \
  || die "missing $REPO_ROOT/.venv/bin/python; run 'uv sync' first"

# Stop a previous watcher / host forwarder / sidecar so the port allocator
# sees the loopback ports as free and reuses the same allocation.
if [ -f "$WATCHER_PID_FILE" ]; then
  kill "$(cat "$WATCHER_PID_FILE")" 2>/dev/null || true
fi
if [ -f "$PID_FILE" ]; then
  while read -r p; do
    [ -n "$p" ] && kill "$p" 2>/dev/null || true
  done < "$PID_FILE"
fi
docker rm -f "$DIND_SIDECAR_NAME" >/dev/null 2>&1 || true

# Force a fresh port allocation write so the watcher knows exactly when to
# start the host forwarders.
mv -f "$PORTS_FILE" "$STATE_DIR/ports.json.prev" 2>/dev/null || true
rm -f "$LOG_DIR/init.log" "$LOG_DIR/forwarders.log" "$PID_FILE"

nohup env -u DOCKER_HOST "$SCRIPT_DIR/watch-and-forward.sh" >/dev/null 2>&1 &
echo $! > "$WATCHER_PID_FILE"

cd "$REPO_ROOT" || exit 1
export DOCKER_HOST="$DOCKER_HOST_DIND"
export HARBORSTACK_IMAGE__MIRROR="$MIRROR"

# aarch64 ClickHouse patch: keep the patch pipeline's temporary files under
# the cache dir that is bind-mounted into dind.  The smoke test bind-mounts
# the patched binary by absolute path, and the inner daemon must see it.
export TMPDIR="$CACHE_DIR/tmp"
mkdir -p "$TMPDIR"

echo "==> harborstack init against dind ($DOCKER_HOST_DIND)"
set +e
.venv/bin/harborstack init 2>&1 | tee "$LOG_DIR/init.log"
rc=${PIPESTATUS[0]}
set -e
echo "exit_code=$rc" >> "$LOG_DIR/init.log"

if [ -f "$WATCHER_PID_FILE" ]; then
  kill "$(cat "$WATCHER_PID_FILE")" 2>/dev/null || true
fi

if [ "$rc" -eq 0 ] && [ -f "$PORTS_FILE" ]; then
  WEB_PORT=$("$REPO_ROOT/.venv/bin/python" - "$PORTS_FILE" <<'PY'
import json
import sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("web", ""))
PY
)
  [ -n "$WEB_PORT" ] && echo "==> Langfuse: http://localhost:$WEB_PORT"
fi

exit "$rc"
