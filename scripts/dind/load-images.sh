#!/bin/bash
# Stream the six harborstack images from the host Docker daemon into dind.
#
# Run this after:
#   1. the host has the images (harborstack install on the host), and
#   2. start-dind.sh has brought up the dind daemon.
set -euo pipefail

source "$(dirname "$0")/common.sh"

VENV_PY="$REPO_ROOT/.venv/bin/python"
[ -x "$VENV_PY" ] || die "missing $VENV_PY; run 'uv sync' in $REPO_ROOT first"

mapfile -t REFS < <(
  HARBORSTACK_IMAGE__MIRROR="$MIRROR" "$VENV_PY" - "$REPO_ROOT" <<'PY'
import os
import sys
import tomllib

root = sys.argv[1]
with open(os.path.join(root, "harborstack.toml"), "rb") as f:
    cfg = tomllib.load(f)
mirror = os.environ.get("HARBORSTACK_IMAGE__MIRROR") or cfg.get("image", {}).get("mirror", "")

def ref(section, key):
    value = cfg[section][key]
    return f"{mirror}/{value}" if mirror else value

for section, key in (
    ("langfuse", "image"),
    ("langfuse", "worker_image"),
    ("clickhouse", "image"),
    ("minio", "image"),
    ("postgres", "image"),
    ("redis", "image"),
):
    print(ref(section, key))
PY
)

[ "${#REFS[@]}" -eq 6 ] || die "expected 6 image refs, got ${#REFS[@]}"

missing_dind=()
for ref in "${REFS[@]}"; do
  DOCKER_HOST="$DOCKER_HOST_DIND" docker image inspect "$ref" >/dev/null 2>&1 \
    || missing_dind+=("$ref")
done
if [ "${#missing_dind[@]}" -eq 0 ]; then
  echo "==> all 6 images already in dind; nothing to load"
  exit 0
fi

missing=()
for ref in "${REFS[@]}"; do
  docker image inspect "$ref" >/dev/null 2>&1 || missing+=("$ref")
done

if [ "${#missing[@]}" -gt 0 ]; then
  echo "ERROR: these images are not in the host Docker daemon:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  echo >&2
  echo "Populate them first (this only pulls images, it does not start containers):" >&2
  echo "  cd $REPO_ROOT" >&2
  echo "  HARBORSTACK_IMAGE__MIRROR=$MIRROR .venv/bin/harborstack install" >&2
  exit 1
fi

echo "==> loading ${#REFS[@]} images into dind ($DOCKER_HOST_DIND)"
docker save "${REFS[@]}" | DOCKER_HOST="$DOCKER_HOST_DIND" docker load

echo "==> verifying images in dind"
for ref in "${REFS[@]}"; do
  DOCKER_HOST="$DOCKER_HOST_DIND" docker image inspect "$ref" >/dev/null 2>&1 \
    || die "image missing in dind after load: $ref"
  echo "  ok $ref"
done

echo
echo "NOTE: docker save/load does not preserve RepoDigests. harborstack init will"
echo "      fetch the manifest metadata from the mirror; the image layers are"
echo "      already present in dind, so this should be quick."
