#!/bin/bash
# Prepare the six images on the host and load them into dind.
set -euo pipefail

source "$(dirname "$0")/common.sh"

[ -x "$REPO_ROOT/.venv/bin/harborstack" ] \
  || die "missing $REPO_ROOT/.venv/bin/harborstack; run scripts/dind/setup-stack-venv.sh first"

# Host install must use the host daemon.
unset DOCKER_HOST
export HARBORSTACK_IMAGE__MIRROR="$MIRROR"

"$REPO_ROOT/.venv/bin/harborstack" install
"$SCRIPT_DIR/load-images.sh"
