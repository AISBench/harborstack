"""环境契约 var/env-manifest.json。

这是 init 对「我到底部署了什么」的唯一权威声明：平台探测结果、镜像 digest、
补丁产物指纹、实际分配的端口、compose 资产指纹。display 只读它，不猜。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from harborstack import config as cfgmod
from harborstack import fsutil
from harborstack import lockfile
from harborstack.deploy import all_assets

SCHEMA = 1
NAME = "env-manifest.json"

PREPARED, READY, FAILED = "prepared", "ready", "failed"


def path_for(cfg: dict) -> Path:
    return cfgmod.expand_path(cfg["paths"]["var_dir"]) / NAME


def compose_fingerprint() -> dict[str, str]:
    return {p.name: fsutil.sha256_file(p) for p in all_assets()}


def build(
    cfg: dict,
    lock: lockfile.Lock,
    platform: dict,
    ports: dict[str, int],
    binary: dict | None,
    state: str,
) -> dict:
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": cfg.get("stack", {}).get("env", "default"),
        "platform": platform,
        "images": {name: {"digest": pin.digest} for name, pin in lock.pins.items()},
        "binaries": {"clickhouse": binary} if binary else {},
        "ports": ports,
        "compose": compose_fingerprint(),
        "state": state,
    }


def load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if data.get("schema") == SCHEMA else None


def save(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
