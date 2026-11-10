#!/bin/bash
# Prepare the optional harbor workspace for evaluation.
#
# This is deliberately separate from up.sh:
#   up.sh        -> Langfuse observability stack only
#   setup-harbor -> harbor repository path + venv + CLI check
#
# Harbor path resolution (highest priority first):
#   1. HARBORSTACK_PATHS__HARBOR_REPO
#   2. [paths].harbor_repo in harborstack.toml
#   3. ~/code/harbor
#
# Usage:
#   setup-harbor.sh          # runtime env: uv sync --no-dev
#   setup-harbor.sh --full   # full env: uv sync (dev + cloud adapters)
set -euo pipefail

source "$(dirname "$0")/common.sh"

MODE="runtime"
case "${1:-}" in
  "") ;;
  --full) MODE="full" ;;
  *) die "usage: setup-harbor.sh [--full]" ;;
esac

# BASE_PY is used to create the harbor venv; READ_PY is used to read
# harborstack.toml (the harborstack venv already has tomllib when present).
BASE_PY="${HARBORSTACK_PYTHON:-$HOME/.local/opt/python-3.13/bin/python3.13}"
if [ ! -x "$BASE_PY" ]; then
  BASE_PY=$(command -v python3.13 || command -v python3.12 || true)
fi
[ -n "$BASE_PY" ] && [ -x "$BASE_PY" ] || die "python >=3.12 not found; see docs/runbook-toolchain.md"
READ_PY="$BASE_PY"
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  READ_PY="$REPO_ROOT/.venv/bin/python"
fi
command -v uv >/dev/null 2>&1 || die "uv not found; see docs/runbook-toolchain.md"
# Same mirror default as setup-stack-venv.sh.  The lockfile is restored after
# sync, so this only changes where wheels are downloaded from.
export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-https://repo.huaweicloud.com/repository/pypi/simple}"
export UV_PYTHON_DOWNLOADS=never

HARBOR_REPO="${HARBORSTACK_PATHS__HARBOR_REPO:-}"
if [ -z "$HARBOR_REPO" ] && [ -f "$REPO_ROOT/harborstack.toml" ]; then
  HARBOR_REPO=$("$READ_PY" - "$REPO_ROOT/harborstack.toml" <<'PY'
import sys
try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit(0)
with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)
print(cfg.get("paths", {}).get("harbor_repo", ""))
PY
)
fi
HARBOR_REPO="${HARBOR_REPO:-$HOME/code/harbor}"
case "$HARBOR_REPO" in
  # Quote the tilde in the pattern; an unquoted "~/" in ${var#...} is
  # tilde-expanded and would produce "$HOME/~/...".
  "~/"*) HARBOR_REPO="$HOME/${HARBOR_REPO#"~/"}" ;;
esac

echo "==> harbor repo: $HARBOR_REPO"
[ -d "$HARBOR_REPO" ] || die "harbor repo not found: $HARBOR_REPO (set HARBORSTACK_PATHS__HARBOR_REPO or harborstack.toml)"
[ -f "$HARBOR_REPO/pyproject.toml" ] || die "not a harbor repo (missing pyproject.toml): $HARBOR_REPO"
[ -f "$HARBOR_REPO/uv.lock" ] || die "not a harbor repo (missing uv.lock): $HARBOR_REPO"

if [ -f "$HARBOR_REPO/.git/HEAD" ]; then
  head=$(cat "$HARBOR_REPO/.git/HEAD" 2>/dev/null || true)
  case "$head" in
    "ref: refs/heads/"*) echo "==> branch: ${head#ref: refs/heads/}" ;;
    *) echo "==> HEAD: $head" ;;
  esac
  echo "==> note: the jiuwenswarm runbook expects branch feat/jiuwenswarm_adapter"
fi

echo "==> syncing harbor ($MODE)"
backup=$(mktemp)
cp -p "$HARBOR_REPO/uv.lock" "$backup"
restore_lock() {
  cp -p "$backup" "$HARBOR_REPO/uv.lock"
  rm -f "$backup"
}
# Restore on success, failure, Ctrl-C or TERM: the mirror can rewrite uv.lock
# while resolving, and we do not want that rewrite in the harbor repo.
trap restore_lock EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ "$MODE" = "full" ]; then
  (cd "$HARBOR_REPO" && uv sync --python "$BASE_PY") || die "harbor uv sync failed"
else
  (cd "$HARBOR_REPO" && uv sync --no-dev --python "$BASE_PY") || die "harbor uv sync failed"
fi

[ -x "$HARBOR_REPO/.venv/bin/harbor" ] || die "harbor CLI missing: $HARBOR_REPO/.venv/bin/harbor"
echo "==> harbor version: $("$HARBOR_REPO/.venv/bin/harbor" --version)"
echo
echo "Make sure harborstack uses the same path:"
echo "  export HARBORSTACK_PATHS__HARBOR_REPO=$HARBOR_REPO"
echo "  # or set [paths].harbor_repo in harborstack.toml"
echo
echo "For a later evaluation (not run here):"
echo "  export DEEPSEEK_API_KEY=..."
echo "  harborstack run -- run -p /path/to/tasks -a jiuwenswarm -m deepseek/deepseek-flash -e docker -n 1 -y"
