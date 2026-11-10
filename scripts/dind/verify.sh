#!/bin/bash
# Verify the dind stack: doctor, fixture import, status, terminal trace.
set -euo pipefail

source "$(dirname "$0")/common.sh"

cd "$REPO_ROOT" || exit 1
[ -x .venv/bin/harborstack ] \
  || die "missing .venv/bin/harborstack; run scripts/dind/setup-stack-venv.sh first"

export DOCKER_HOST="$DOCKER_HOST_DIND"
export HARBORSTACK_IMAGE__MIRROR="$MIRROR"

# Just after init, doctor's 2s port probe can race with a warming web
# container and report "port occupied by another process".  Wait until the
# same endpoint answers before running the checks.
WEB_PORT=$(
  .venv/bin/python - <<'PY'
import json
from pathlib import Path
try:
    print(json.loads(Path("var/ports.json").read_text())["web"])
except Exception:
    pass
PY
)
WEB_READY=0
if [ -n "$WEB_PORT" ]; then
  echo "==> wait for web health on 127.0.0.1:$WEB_PORT (up to 30s)"
  for _ in $(seq 1 30); do
    if .venv/bin/python - "$WEB_PORT" <<'PY'
import sys
import urllib.request
try:
    url = f"http://127.0.0.1:{sys.argv[1]}/api/public/health"
    with urllib.request.urlopen(url, timeout=2.0) as response:
        raise SystemExit(0 if 200 <= response.status < 300 else 1)
except Exception:
    raise SystemExit(1)
PY
    then
      WEB_READY=1
      break
    fi
    sleep 1
  done
fi
if [ "$WEB_READY" = 0 ]; then
  echo "WARN: web did not answer within 30s; doctor may report a transient port warning" >&2
fi

.venv/bin/harborstack doctor
.venv/bin/harborstack display fixtures/atif-fidelity
"$SCRIPT_DIR/status.sh"
.venv/bin/python scripts/show-trace.py --wait 60
