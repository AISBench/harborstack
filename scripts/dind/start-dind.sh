#!/bin/bash
# Start (or reuse) the isolated Docker-in-Docker daemon used by the dind path.
set -euo pipefail

source "$(dirname "$0")/common.sh"

command -v docker >/dev/null 2>&1 || die "docker not found in PATH"

echo "==> dind container: $DIND_NAME"
echo "==> image:          $DIND_IMAGE"
echo "==> volume:         $DIND_VOLUME"
echo "==> api:            $DOCKER_HOST_DIND"

if ! docker image inspect "$DIND_IMAGE" >/dev/null 2>&1; then
  echo "==> pulling $DIND_IMAGE"
  docker pull "$DIND_IMAGE"
fi

if ! docker image inspect "$DIND_PY_IMAGE" >/dev/null 2>&1; then
  echo "==> pulling $DIND_PY_IMAGE (for the dind-side forwarder)"
  docker pull "$DIND_PY_IMAGE"
fi

if ! docker volume inspect "$DIND_VOLUME" >/dev/null 2>&1; then
  echo "==> creating volume $DIND_VOLUME"
  docker volume create "$DIND_VOLUME" >/dev/null
fi

if docker inspect "$DIND_NAME" >/dev/null 2>&1; then
  state=$(docker inspect -f '{{.State.Status}}' "$DIND_NAME")
  if [ "$state" = "running" ]; then
    echo "==> already running"
  else
    echo "==> starting existing container (state=$state)"
    docker start "$DIND_NAME" >/dev/null
  fi

  # Older dind containers created before the aarch64 patch support may lack
  # the repo/cache bind mounts.  Warn instead of silently producing a broken
  # ClickHouse container later.
  mounts=$(docker inspect -f '{{range .Mounts}}{{.Destination}}{{"\n"}}{{end}}' "$DIND_NAME" 2>/dev/null || true)
  for path in "$REPO_ROOT" "$CACHE_DIR"; do
    if ! printf '%s\n' "$mounts" | grep -Fxq "$path"; then
      echo "WARN: existing dind '$DIND_NAME' lacks bind mount $path"
      echo "      aarch64 ClickHouse patch may fail; recreate the dind container (volume preserved)."
    fi
  done
else
  echo "==> creating container"
  docker run -d --name "$DIND_NAME" \
    --privileged \
    --cpus="$DIND_CPUS" --memory="$DIND_MEMORY" \
    --restart=no \
    -e DOCKER_TLS_CERTDIR= \
    -p "127.0.0.1:${DIND_API_PORT}:2375" \
    -v "$DIND_VOLUME:/var/lib/docker" \
    -v "$REPO_ROOT:$REPO_ROOT:ro" \
    -v "$CACHE_DIR:$CACHE_DIR:ro" \
    "$DIND_IMAGE" \
    --host=tcp://0.0.0.0:2375 --host=unix:///var/run/docker.sock
fi

echo "==> waiting for dind daemon"
for _ in $(seq 1 60); do
  if DOCKER_HOST="$DOCKER_HOST_DIND" docker info >/dev/null 2>&1; then
    DOCKER_HOST="$DOCKER_HOST_DIND" docker version \
      --format '==> dind ready: client={{.Client.Version}} server={{.Server.Version}}'
    echo
    echo "Use:"
    echo "  export DOCKER_HOST=$DOCKER_HOST_DIND"
    echo "  export HARBORSTACK_IMAGE__MIRROR=$MIRROR"
    exit 0
  fi
  sleep 1
done

echo "ERROR: dind daemon did not become ready" >&2
docker logs --tail 80 "$DIND_NAME" >&2 || true
exit 1
