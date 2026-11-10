#!/bin/bash
# Shared configuration for the dind (Docker-in-Docker) path.
# Source this file from the other scripts in this directory.
set -u

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

# All of these can be overridden from the environment.
DIND_NAME=${DIND_NAME:-harborstack-dind}
DIND_IMAGE=${DIND_IMAGE:-docker.m.daocloud.io/library/docker:27.5.1-dind}
DIND_API_PORT=${DIND_API_PORT:-23750}
DIND_VOLUME=${DIND_VOLUME:-harborstack-dind-data}
DIND_CPUS=${DIND_CPUS:-12}
DIND_MEMORY=${DIND_MEMORY:-16g}
DIND_PY_IMAGE=${DIND_PY_IMAGE:-docker.m.daocloud.io/library/python:3.13-alpine}
DIND_SIDECAR_NAME=${DIND_SIDECAR_NAME:-harborstack-dind-fwd}

# Optional per-deployment overrides (e.g. reuse an existing dind container).
STATE_DIR="$REPO_ROOT/var/dind"
if [ -f "$STATE_DIR/env" ]; then
  # shellcheck disable=SC1091
  . "$STATE_DIR/env"
fi

DOCKER_HOST_DIND="tcp://127.0.0.1:${DIND_API_PORT}"

# Python reader for harborstack.toml, used for both image.mirror and
# paths.cache_dir below.
PY_READ="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$PY_READ" ]; then
  PY_READ=$(command -v python3.13 || command -v python3.12 || true)
fi

# Keep the image prefix consistent with harborstack itself.  Priority:
# HARBORSTACK_IMAGE__MIRROR > harborstack.toml [image].mirror > default.
MIRROR="${HARBORSTACK_IMAGE__MIRROR:-}"
if [ -z "$MIRROR" ] && [ -f "$REPO_ROOT/harborstack.toml" ] && [ -n "$PY_READ" ]; then
  MIRROR=$("$PY_READ" - "$REPO_ROOT/harborstack.toml" <<'PY'
import sys
try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit(0)
with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)
print(cfg.get("image", {}).get("mirror", ""))
PY
)
fi
MIRROR="${MIRROR:-docker.m.daocloud.io}"

# Patch cache. Must match harborstack's paths.cache_dir: for aarch64 without
# RCpc, the patch pipeline writes the patched ClickHouse binary and its smoke
# test temp files here, and the dind container mounts this directory at the
# same absolute path so the inner Docker daemon can see them.
CACHE_DIR="${HARBORSTACK_PATHS__CACHE_DIR:-}"
if [ -z "$CACHE_DIR" ] && [ -f "$REPO_ROOT/harborstack.toml" ] && [ -n "$PY_READ" ]; then
  CACHE_DIR=$("$PY_READ" - "$REPO_ROOT/harborstack.toml" <<'PY'
import sys
try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit(0)
with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)
print(cfg.get("paths", {}).get("cache_dir", ""))
PY
)
fi
CACHE_DIR="${CACHE_DIR:-$HOME/.cache/harborstack}"
case "$CACHE_DIR" in
  # Quote the tilde in the pattern; an unquoted "~/" in ${var#...} is
  # tilde-expanded and would produce "$HOME/~/...".
  "~/"*) CACHE_DIR="$HOME/${CACHE_DIR#"~/"}" ;;
esac
mkdir -p "$CACHE_DIR"
CACHE_DIR=$(cd "$CACHE_DIR" && pwd)

PORTS_FILE="$REPO_ROOT/var/ports.json"
LOG_DIR="$STATE_DIR/logs"
PID_FILE="$STATE_DIR/host-forwarders.pids"
WATCHER_PID_FILE="$STATE_DIR/watcher.pid"

mkdir -p "$STATE_DIR" "$LOG_DIR"

die() {
  echo "ERROR: $*" >&2
  exit 1
}
