"""lock.json 的加载与校验：所有外部依赖的冻结坐标。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class LockError(Exception):
    """lock.json 不合法，消息里必须说明缺什么、怎么补。"""


@dataclass(frozen=True)
class Pin:
    name: str
    ref: str
    digest: str
    version: str
    origin: str = "upstream"


@dataclass(frozen=True)
class Lock:
    schema: int
    frozen_at: str
    pins: dict[str, Pin] = field(default_factory=dict)
    artifacts: dict[str, dict] = field(default_factory=dict)
    compose: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "Lock":
        p = Path(path)
        if not p.exists():
            raise LockError(f"找不到 {p}；请从仓库根执行，或用 --lock 指定")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise LockError(f"{p} 不是合法 JSON：{e}") from e

        schema = raw.get("schema")
        if schema != 1:
            raise LockError(f"{p} 的 schema 必须是 1，当前：{schema}")

        pins_raw = raw.get("pins") or {}
        required = {"langfuse_web", "langfuse_worker", "clickhouse", "minio", "postgres", "redis"}
        missing = required - set(pins_raw)
        if missing:
            raise LockError(f"{p} 缺少 pins：{', '.join(sorted(missing))}")

        pins: dict[str, Pin] = {}
        for name, item in pins_raw.items():
            digest = item.get("digest", "")
            if not DIGEST_RE.match(digest):
                raise LockError(f"pins.{name}.digest 不是 sha256:<64hex>：{digest!r}")
            if not item.get("ref"):
                raise LockError(f"pins.{name}.ref 不能为空")
            pins[name] = Pin(
                name=name,
                ref=item["ref"],
                digest=digest,
                version=item.get("version", ""),
                origin=item.get("origin", "upstream"),
            )

        artifacts = raw.get("artifacts") or {}
        patch = artifacts.get("clickhouse_patch")
        if patch:
            for key in ("source_sha256", "output_sha256", "patcher"):
                if not patch.get(key):
                    raise LockError(f"artifacts.clickhouse_patch 缺少 {key}")

        compose_raw = raw.get("compose") or {}
        compose = compose_raw.get("files") or {}
        for name, sha in compose.items():
            if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
                raise LockError(f"compose.files.{name} 不是 64 位小写 hex：{sha!r}")

        return cls(schema=schema, frozen_at=raw.get("frozen_at", ""), pins=pins,
                   artifacts=artifacts, compose=compose, path=p)

    def pin(self, name: str) -> Pin:
        if name not in self.pins:
            raise LockError(f"lock.json 里没有 {name}")
        return self.pins[name]

    def patched_binary_stem(self) -> str:
        """补丁产物在缓存里的文件名主干：镜像 digest 前缀 + patcher 版本。"""
        patch = self.artifacts.get("clickhouse_patch") or {}
        digest = self.pin("clickhouse").digest.split(":", 1)[1][:12]
        return f"clickhouse-{digest}-p{patch.get('patcher', '0')}"


def digest_matches(expected: str, actual: str | None) -> bool:
    return bool(actual) and actual.split("@")[-1] == expected
