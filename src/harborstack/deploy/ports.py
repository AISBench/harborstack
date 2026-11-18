"""端口分配器：从配置区间里挑出真正能 bind 的主机端口。

为什么必须做：docker 已占用的端口，IDE 的端口中继会判为「不可用」且不给任何
提示。分配器在起栈前就把冲突规避掉，并把结果写回 .env 与 manifest，保证容器、
manifest、用户看到的链接三者一致。
"""

from __future__ import annotations

from collections.abc import Callable

from harborstack.env import probe

# 逻辑名 -> 容器内监听端口
SERVICES: dict[str, int] = {
    "web": 3000,
    "worker": 3030,
    "clickhouse_http": 8123,
    "clickhouse_native": 9000,
    "minio_api": 9000,
    "minio_console": 9001,
    "redis": 6379,
    "postgres": 5432,
}

ORDER = tuple(SERVICES)

# 逻辑名 -> .env 变量名
ENV_KEYS: dict[str, str] = {
    "web": "HS_PORT_WEB",
    "worker": "HS_PORT_WORKER",
    "clickhouse_http": "HS_PORT_CH_HTTP",
    "clickhouse_native": "HS_PORT_CH_NATIVE",
    "minio_api": "HS_PORT_MINIO_API",
    "minio_console": "HS_PORT_MINIO_CONSOLE",
    "redis": "HS_PORT_REDIS",
    "postgres": "HS_PORT_POSTGRES",
}


class PortError(Exception):
    """区间内凑不出足够的端口。"""


def allocate(
    cfg: dict,
    saved: dict[str, int] | None = None,
    is_free: Callable[[int], bool] = probe.port_free,
) -> dict[str, int]:
    """按固定顺序分配端口。

    优先级：上一次分配的端口（仍空闲）> ports.preferred > 区间内第一个空闲端口。
    复用上次结果是为了让重复执行 init 不会把已经起好的栈换成另一套端口。
    """
    lo, hi = cfg["ports"]["range"]
    preferred = cfg["ports"].get("preferred") or {}
    saved = saved or {}

    used: set[int] = set()
    result: dict[str, int] = {}

    for name in ORDER:
        picked: int | None = None

        last = saved.get(name)
        if isinstance(last, int) and lo <= last <= hi and last not in used and is_free(last):
            picked = last

        if picked is None:
            want = preferred.get(name)
            if isinstance(want, int) and want not in used and is_free(want):
                picked = want

        if picked is None:
            for port in range(lo, hi + 1):
                if port not in used and is_free(port):
                    picked = port
                    break

        if picked is None:
            raise PortError(
                f"ports.range [{lo}, {hi}] 内找不到 {len(ORDER)} 个可用端口；"
                "缩小范围前请确认没有其他服务占用该区间"
            )
        used.add(picked)
        result[name] = picked

    return result
