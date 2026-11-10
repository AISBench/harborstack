#!/bin/bash
# One-command dind path: venvs -> dind -> images -> init -> verify.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

echo "==> 1/5 harborstack venv"
"$SCRIPT_DIR/setup-stack-venv.sh"
echo "==> 2/5 dind"
"$SCRIPT_DIR/start-dind.sh"
echo "==> 3/5 images"
"$SCRIPT_DIR/install-images.sh"
echo "==> 4/5 init"
"$SCRIPT_DIR/run-init.sh"
echo "==> 5/5 verify"
"$SCRIPT_DIR/verify.sh"

echo
echo "==> stack is up. For a later harbor evaluation workspace:"
echo "    scripts/dind/setup-harbor.sh"
