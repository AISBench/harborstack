"""部署资产：vendored 的上游 compose 快照 + harborstack 隔离层。

上游快照来自 github.com/langfuse/langfuse，commit 7526ede9（MIT 许可），
只用于锁定服务拓扑与环境变量名；运行时不访问上游仓库，不做任何拉取。
快照的 sha256 与来源 commit 记录在 lock.json 的 compose 段，由 doctor 校验，
手工改动会立刻被检出。
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LANGFUSE_DIR = BASE_DIR / "langfuse"
BASE_COMPOSE = LANGFUSE_DIR / "docker-compose.yml"
ISOLATION_COMPOSE = BASE_DIR / "docker-compose.isolation.yml"
PATCH_COMPOSE = BASE_DIR / "docker-compose.patch.yml"
SKIP_CHECKSUM_XML = BASE_DIR / "clickhouse-skip-checksum.xml"

# 未穿刺时只需要前两个文件；需要穿刺时才叠加第三个
ALWAYS_FILES = (BASE_COMPOSE, ISOLATION_COMPOSE)


def compose_files(patched: bool) -> tuple[Path, ...]:
    return ALWAYS_FILES + (PATCH_COMPOSE,) if patched else ALWAYS_FILES


def all_assets() -> tuple[Path, ...]:
    return ALWAYS_FILES + (PATCH_COMPOSE, SKIP_CHECKSUM_XML)
