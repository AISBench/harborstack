#!/bin/bash
# Create/refresh the harborstack virtualenv only.
# The harbor workspace venv is prepared separately by setup-harbor.sh.
set -euo pipefail

source "$(dirname "$0")/common.sh"

export UV_DEFAULT_INDEX=https://repo.huaweicloud.com/repository/pypi/simple
export UV_PYTHON_DOWNLOADS=never

PY="${HARBORSTACK_PYTHON:-$HOME/.local/opt/python-3.13/bin/python3.13}"
if [ ! -x "$PY" ]; then
  PY=$(command -v python3.13 || command -v python3.12 || true)
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  die "python >=3.12 not found; set HARBORSTACK_PYTHON or see docs/runbook-toolchain.md"
fi
command -v uv >/dev/null 2>&1 || die "uv not found; see docs/runbook-toolchain.md"
echo "==> python: $PY"

sync_repo() {
  local dir=$1
  shift
  [ -f "$dir/uv.lock" ] || die "missing $dir/uv.lock"
  (
    cd "$dir" || exit 1
    local backup
    backup=$(mktemp)
    cp -p uv.lock "$backup"
    restore_lock() {
      cp -p "$backup" uv.lock
      rm -f "$backup"
    }
    # Restore on success, failure, Ctrl-C or TERM: the mirror can rewrite
    # uv.lock while resolving.
    trap restore_lock EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    uv sync "$@" --python "$PY"
  )
}

echo "==> syncing harborstack"
sync_repo "$REPO_ROOT"

[ -x "$REPO_ROOT/.venv/bin/harborstack" ] || die "harborstack entrypoint missing"
echo "==> harborstack venv ready"
echo "==> harbor workspace is separate: run scripts/dind/setup-harbor.sh for evaluation"
