"""配置加载：内置默认值 <- harborstack.toml <- 环境变量 <- CLI --set。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

ENV_PREFIX = "HARBORSTACK_"
ENV_SEP = "__"


class ConfigError(Exception):
    """配置不合法，消息里必须给出可执行的修复建议。"""


BUILTIN_DEFAULTS: dict[str, Any] = {
    "stack": {"env": "default"},
    "platform": {"allow_patch": True},
    "paths": {
        "harbor_repo": "~/code/harbor",
        "cache_dir": "~/.cache/harborstack",
        "var_dir": "./var",
    },
    "ports": {"range": [18900, 18999], "preferred": {"web": 18930}},
    "langfuse": {
        "image": "langfuse/langfuse:4",
        "worker_image": "langfuse/langfuse-worker:4",
        "memory": "2g",
        "cpus": 2.0,
        "security_mode": "default",
        "admin": {"email": "", "name": "", "password": ""},
    },
    "clickhouse": {"image": "clickhouse/clickhouse-server:25.12-alpine", "bin_cache": True},
    "minio": {"image": "bitnamilegacy/minio:latest"},
    "postgres": {"image": "library/postgres:16"},
    "redis": {"image": "library/redis:7-alpine"},
    "image": {"mirror": ""},
    "export": {"mode": "full", "idempotent": True},
    "bundle": {"dir": "", "mode": "docker-save", "include_patched_bin": True},
    "report": {"path": "./var/report.json"},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _parse_scalar(raw: str) -> Any:
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if raw.strip().startswith(("[", "{")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return raw


def _assign_dotted(target: dict, dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    cur = target
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value


def _env_overrides(env: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, raw in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        dotted = key[len(ENV_PREFIX):].lower().split(ENV_SEP)
        if not all(dotted):
            continue
        _assign_dotted(out, ".".join(dotted), _parse_scalar(raw))
    return out


def load_config(
    path: str | Path | None = None,
    sets: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """按优先级合并配置。path=None 时使用当前目录的 harborstack.toml（存在才读）。"""
    cfg = copy.deepcopy(BUILTIN_DEFAULTS)

    toml_path = Path(path) if path else Path.cwd() / "harborstack.toml"
    if path and not toml_path.exists():
        raise ConfigError(f"配置文件不存在：{toml_path}")
    if toml_path.exists():
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"TOML 解析失败：{toml_path}：{e}") from e
        cfg = deep_merge(cfg, data)

    cfg = deep_merge(cfg, _env_overrides(env if env is not None else os.environ))

    for item in sets or []:
        if "=" not in item:
            raise ConfigError(f"--set 需要 KEY=VALUE 形式，收到：{item}")
        k, raw = item.split("=", 1)
        _assign_dotted(cfg, k.strip(), _parse_scalar(raw))

    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    env = cfg["stack"].get("env")
    if not (isinstance(env, str) and env and all(c.isalnum() or c == "-" for c in env)):
        raise ConfigError(f"stack.env 只能包含字母数字与连字符，当前：{env!r}")

    mode = cfg["langfuse"].get("security_mode")
    if mode not in ("default", "generated"):
        raise ConfigError(f"langfuse.security_mode 只能是 default 或 generated，当前：{mode}")

    rng = cfg["ports"].get("range")
    if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, int) for x in rng)):
        raise ConfigError("ports.range 必须是两个整数，例如 [18900, 18999]")
    if rng[0] >= rng[1]:
        raise ConfigError(f"ports.range 起点必须小于终点，当前：{rng}")

    web = cfg["ports"].get("preferred", {}).get("web")
    if not isinstance(web, int) or not (rng[0] <= web <= rng[1]):
        raise ConfigError(f"ports.preferred.web 必须落在 ports.range 内，当前：{web}")

    if cfg["export"].get("mode") not in ("full", "structural"):
        raise ConfigError("export.mode 只能是 full 或 structural")

    if cfg["bundle"].get("mode") not in ("docker-save", "oci"):
        raise ConfigError("bundle.mode 只能是 docker-save 或 oci")


def expand_path(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value))).resolve()


IMAGE_KEYS: dict[str, tuple[str, str]] = {
    "langfuse_web": ("langfuse", "image"),
    "langfuse_worker": ("langfuse", "worker_image"),
    "clickhouse": ("clickhouse", "image"),
    "minio": ("minio", "image"),
    "postgres": ("postgres", "image"),
    "redis": ("redis", "image"),
}


def image_ref(cfg: dict, name: str) -> str:
    """把 mirror 前缀拼到镜像 ref 上。name 取 lock.json 的 pin 名。"""
    section, key = IMAGE_KEYS[name]
    ref = cfg[section][key]
    mirror = cfg["image"].get("mirror") or ""
    return f"{mirror}/{ref}" if mirror else ref
