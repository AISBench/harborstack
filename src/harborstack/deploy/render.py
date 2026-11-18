"""把配置 + lock + 端口 + 密钥渲染成 docker compose 可消费的 .env。

渲染结果落在 paths.var_dir/.env（0600）。所有 compose 变量都以 HS_ 开头，
与上游 langfuse 自己的变量（NEXTAUTH_URL / DATABASE_URL 等）分开，避免命名冲突。
"""

from __future__ import annotations

import json
import os
import secrets as pysecrets
import urllib.parse
import uuid
from pathlib import Path

from harborstack import config as cfgmod
from harborstack import lockfile
from harborstack.deploy import ports as portsmod

ENV_NAME = ".env"
SECRETS_NAME = "secrets.json"
PORTS_NAME = "ports.json"

ORG_ID = "harborstack-org"
ORG_NAME = "harborstack"
PROJECT_ID = "harborstack-project"
PROJECT_NAME = "harbor-otel"

# security_mode = "default"：固定口令，仅适合只绑回环的调试形态
DEFAULT_SECRETS: dict[str, str] = {
    "postgres_password": "postgres",
    "clickhouse_password": "clickhouse",
    "redis_auth": "myredissecret",
    "minio_password": "miniosecret",
    "salt": "mysalt",
    "encryption_key": "0" * 64,
    "nextauth_secret": "mysecret",
    "public_key": "pk-lf-11111111-1111-4111-8111-111111111111",
    "secret_key": "sk-lf-22222222-2222-4222-8222-222222222222",
}


def project_name(cfg: dict) -> str:
    env = cfg.get("stack", {}).get("env", "default")
    return f"harborstack-{env}"


def _random_hex(nbytes: int) -> str:
    return pysecrets.token_hex(nbytes)


def _existing_secrets(cfg: dict, var_dir: Path) -> dict[str, str] | None:
    """已持久化且 mode 一致的密钥；缺失、损坏或 mode 变更都返回 None。"""
    path = var_dir / SECRETS_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or data.get("mode") != cfg["langfuse"]["security_mode"]:
        return None
    values = data.get("values")
    if not isinstance(values, dict) or not values:
        return None
    return values


def secrets_will_regenerate(cfg: dict, var_dir: Path) -> bool:
    """本次 init 是否会重新生成密钥（与 load_secrets 的复用判定保持一致）。"""
    return _existing_secrets(cfg, var_dir) is None


def load_secrets(cfg: dict, var_dir: Path) -> dict[str, str]:
    """读取或生成密钥。generated 模式下首次生成后持久化，重复 init 不变。"""
    path = var_dir / SECRETS_NAME
    mode = cfg["langfuse"]["security_mode"]

    existing = _existing_secrets(cfg, var_dir)
    if existing is not None:
        return existing

    if mode == "default":
        values = dict(DEFAULT_SECRETS)
    else:
        values = {
            "postgres_password": _random_hex(12),
            "clickhouse_password": _random_hex(12),
            "redis_auth": _random_hex(12),
            "minio_password": _random_hex(12),
            "salt": _random_hex(16),
            "encryption_key": _random_hex(32),
            "nextauth_secret": _random_hex(32),
            "public_key": f"pk-lf-{uuid.uuid4()}",
            "secret_key": f"sk-lf-{uuid.uuid4()}",
        }

    _write_private(path, json.dumps({"mode": mode, "values": values}, indent=2))
    return values


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def build_env(
    cfg: dict,
    lock: lockfile.Lock,
    allocated: dict[str, int],
    sec: dict[str, str],
    clickhouse_bin: Path | None,
    skip_checksum_xml: Path,
) -> dict[str, str]:
    web = allocated["web"]
    admin = cfg["langfuse"]["admin"]
    lf = cfg["langfuse"]

    pg_user = "postgres"
    pg_db = "postgres"
    database_url = (
        f"postgresql://{pg_user}:{urllib.parse.quote(sec['postgres_password'], safe='')}"
        f"@postgres:5432/{pg_db}"
    )

    env: dict[str, str] = {
        # --- Langfuse 自身 ---
        "NEXTAUTH_URL": f"http://localhost:{web}",
        "TELEMETRY_ENABLED": "false",
        "SALT": sec["salt"],
        "ENCRYPTION_KEY": sec["encryption_key"],
        "NEXTAUTH_SECRET": sec["nextauth_secret"],
        "DATABASE_URL": database_url,
        "POSTGRES_USER": pg_user,
        "POSTGRES_PASSWORD": sec["postgres_password"],
        "POSTGRES_DB": pg_db,
        "CLICKHOUSE_USER": "clickhouse",
        "CLICKHOUSE_PASSWORD": sec["clickhouse_password"],
        "REDIS_AUTH": sec["redis_auth"],
        "MINIO_ROOT_USER": "minio",
        "MINIO_ROOT_PASSWORD": sec["minio_password"],
        # 媒体文件对浏览器可见的地址：必须指向分配后的 minio 主机端口
        "LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT": f"http://localhost:{allocated['minio_api']}",
        # --- 首次启动自动建组织 / 项目 / API Key / 登录用户 ---
        "LANGFUSE_INIT_ORG_ID": ORG_ID,
        "LANGFUSE_INIT_ORG_NAME": ORG_NAME,
        "LANGFUSE_INIT_PROJECT_ID": PROJECT_ID,
        "LANGFUSE_INIT_PROJECT_NAME": PROJECT_NAME,
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": sec["public_key"],
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": sec["secret_key"],
        "LANGFUSE_INIT_USER_EMAIL": admin["email"],
        "LANGFUSE_INIT_USER_NAME": admin["name"],
        "LANGFUSE_INIT_USER_PASSWORD": admin["password"],
        # --- harborstack 隔离层 ---
        "HS_SKIP_CHECKSUM_XML": str(skip_checksum_xml),
        "HS_MEM_LANGFUSE": lf["memory"],
        "HS_CPUS_LANGFUSE": str(lf["cpus"]),
        "HS_MEM_CLICKHOUSE": "4g",
        "HS_CPUS_CLICKHOUSE": "2.0",
        "HS_MEM_MINIO": "1g",
        "HS_CPUS_MINIO": "1.0",
        "HS_MEM_REDIS": "512m",
        "HS_CPUS_REDIS": "1.0",
        "HS_MEM_POSTGRES": "1g",
        "HS_CPUS_POSTGRES": "1.0",
    }

    env["HS_IMAGE_LANGFUSE"] = cfgmod.image_ref(cfg, "langfuse_web")
    env["HS_IMAGE_LANGFUSE_WORKER"] = cfgmod.image_ref(cfg, "langfuse_worker")
    env["HS_IMAGE_CLICKHOUSE"] = cfgmod.image_ref(cfg, "clickhouse")
    env["HS_IMAGE_MINIO"] = cfgmod.image_ref(cfg, "minio")
    env["HS_IMAGE_POSTGRES"] = cfgmod.image_ref(cfg, "postgres")
    env["HS_IMAGE_REDIS"] = cfgmod.image_ref(cfg, "redis")

    for name, key in portsmod.ENV_KEYS.items():
        env[key] = str(allocated[name])

    if clickhouse_bin is not None:
        env["HS_CLICKHOUSE_BIN"] = str(clickhouse_bin)

    return env


def write_env(var_dir: Path, env: dict[str, str]) -> Path:
    lines = ["# 由 harborstack init 生成，请勿手改；重复 init 会按配置重新渲染"]
    lines += [f"{k}={v}" for k, v in env.items()]
    path = var_dir / ENV_NAME
    _write_private(path, "\n".join(lines) + "\n")
    return path


def read_ports(var_dir: Path) -> dict[str, int]:
    path = var_dir / PORTS_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: int(v) for k, v in data.items() if k in portsmod.SERVICES}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def write_ports(var_dir: Path, allocated: dict[str, int]) -> Path:
    path = var_dir / PORTS_NAME
    _write_private(path, json.dumps(allocated, indent=2) + "\n")
    return path


def compose_cmd(cfg: dict, env_file: Path, *args: str, patched: bool = True) -> list[str]:
    from harborstack.deploy import compose_files

    cmd = ["docker", "compose", "-p", project_name(cfg), "--env-file", str(env_file)]
    for path in compose_files(patched):
        cmd += ["-f", str(path)]
    return cmd + list(args)
