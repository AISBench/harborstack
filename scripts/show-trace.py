#!/usr/bin/env python3
"""Print a Langfuse trace as a terminal tree.

Usage:
    show-trace.py [REPORT_JSON | TRACE_ID] [--json] [--wait SECONDS]

By default it reads var/report.json (falling back to the
fixtures/atif-fidelity/report.json), takes the first trace id, and queries the
local Langfuse API using the keys in var/secrets.json.  Keys are never printed.

--wait polls the observations API until it returns data (Langfuse ingestion is
eventually consistent, so a trace can be accepted before its observations are
queryable) or the timeout expires.

Requires the stack to be healthy (the host-side forwarder must be running).
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

try:
    from harborstack.deploy.render import PROJECT_ID
except Exception:  # keep the viewer usable without the package on sys.path
    PROJECT_ID = "harborstack-project"


def web_port() -> int:
    for name in ("ports.json", "env-manifest.json"):
        path = REPO / "var" / name
        if not path.is_file():
            continue
        data = json.loads(path.read_text())
        ports = data.get("ports") if name == "env-manifest.json" else data
        web = (ports or {}).get("web")
        if web:
            return int(web)
    raise SystemExit("cannot determine web port; run harborstack init first")


WEB = web_port()


def load_trace_id(arg: str | None) -> str:
    if arg and not arg.endswith(".json"):
        return arg
    if arg:
        path = Path(arg)
    else:
        path = REPO / "var" / "report.json"
        if not path.is_file():
            path = REPO / "fixtures" / "atif-fidelity" / "report.json"
    data = json.loads(path.read_text())
    for trial in data.get("trials", []):
        trace = (trial.get("trace") or {}).get("id")
        if trace:
            return trace
    raise SystemExit(f"no trace id in {path}")


def api(path: str, auth: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{WEB}{path}",
        headers={"Authorization": "Basic " + auth},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def parse_args(argv: list[str]) -> tuple[list[str], bool, float]:
    positional: list[str] = []
    as_json = False
    wait = 0.0
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--json":
            as_json = True
        elif arg == "--wait":
            if i + 1 >= len(argv):
                raise SystemExit("--wait requires a number of seconds")
            try:
                wait = float(argv[i + 1])
            except ValueError:
                raise SystemExit("--wait expects a number of seconds")
            i += 1
        elif arg.startswith("--wait="):
            try:
                wait = float(arg.split("=", 1)[1])
            except ValueError:
                raise SystemExit("--wait expects a number of seconds")
        elif arg.startswith("--"):
            raise SystemExit(f"unknown option: {arg}")
        else:
            positional.append(arg)
        i += 1
    return positional, as_json, max(wait, 0.0)


def fetch_observations(trace_id: str, auth: str, wait: float) -> list[dict]:
    deadline = time.monotonic() + wait
    last_error: Exception | None = None
    while True:
        try:
            data = api(
                f"/api/public/v2/observations?traceId={trace_id}&limit=1000", auth
            ).get("data", [])
            last_error = None
        except Exception as exc:  # keep polling until the deadline
            data = []
            last_error = exc
        if data or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    if last_error is not None and not data:
        raise last_error
    return data


def main() -> None:
    positional, as_json, wait = parse_args(sys.argv[1:])
    trace_id = load_trace_id(positional[0] if positional else None)
    secrets = json.loads((REPO / "var" / "secrets.json").read_text())["values"]
    auth = base64.b64encode(
        f"{secrets['public_key']}:{secrets['secret_key']}".encode()
    ).decode()
    data = fetch_observations(trace_id, auth, wait)

    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    by_id = {o.get("id"): o for o in data}
    children = defaultdict(list)
    roots = []
    for obs in data:
        parent = obs.get("parentObservationId")
        if parent and parent in by_id:
            children[parent].append(obs)
        else:
            roots.append(obs)

    def start(o):
        return o.get("startTime") or ""

    for values in children.values():
        values.sort(key=start)
    roots.sort(key=start)

    generations = sum(1 for o in data if o.get("type") == "GENERATION")
    spans = sum(1 for o in data if o.get("type") == "SPAN")
    print(f"trace  http://127.0.0.1:{WEB}/project/{PROJECT_ID}/traces/{trace_id}")
    print(f"count  {len(data)}  (generation={generations}, span={spans})")

    def walk(obs, depth: int) -> None:
        timestamp = (obs.get("startTime") or "")[11:23]
        latency = obs.get("latency")
        latency_text = f"{latency:8.3f}s" if isinstance(latency, (int, float)) else "       ?"
        name = obs.get("name") or "-"
        print(f"{'  ' * depth}- {timestamp} {obs.get('type', '?'):<10} {name:<20} {latency_text}")
        for child in children.get(obs.get("id"), []):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)


if __name__ == "__main__":
    main()
