"""健康门禁：init 只有在全部通过后才把 manifest 标成 ready。

设计里 init 成功 = 六容器 healthy + ClickHouse 可执行查询 + web 健康端点 200
+ events_full 已由迁移建出。最后一条是真正的「可写入」前置条件：表不存在，
后面的导入必然失败，所以必须在 init 阶段就卡住，而不是等到 display。
"""

from __future__ import annotations

import json
import subprocess
import time

from harborstack.deploy import render

EXPECTED_SERVICES = ("langfuse-web", "langfuse-worker", "clickhouse", "minio", "redis", "postgres")


def container_name(project: str, service: str) -> str:
    return f"{project}-{service}-1"


def compose_ps(cmd: list[str]) -> list[dict]:
    p = subprocess.run(cmd + ["ps", "--format", "json"], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return []
    out = p.stdout.strip()
    if not out:
        return []
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows


def snapshot(cmd: list[str]) -> dict[str, dict]:
    """service -> {state, health}"""
    result: dict[str, dict] = {}
    for row in compose_ps(cmd):
        service = row.get("Service") or row.get("service")
        if not service:
            continue
        result[service] = {
            "state": row.get("State") or row.get("state") or "",
            "health": row.get("Health") or row.get("health") or "",
        }
    return result


def wait_containers(cmd: list[str], timeout: float = 600, interval: float = 5) -> tuple[bool, str]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        snap = snapshot(cmd)
        bad = []
        for service in EXPECTED_SERVICES:
            info = snap.get(service)
            if not info:
                bad.append(f"{service}=缺失")
                continue
            state = info["state"].lower()
            health = info["health"].lower()
            if state != "running":
                bad.append(f"{service}={state or 'unknown'}")
            elif health and health != "healthy":
                bad.append(f"{service}=running/{health}")
        if not bad:
            return True, "六容器全部 running/healthy"
        last = "，".join(bad)
        time.sleep(interval)
    return False, f"等待超时（{int(timeout)}s），未就绪：{last}"


def clickhouse_query(project: str, sql: str, password: str, timeout: int = 30) -> tuple[bool, str]:
    p = subprocess.run(
        ["docker", "exec", container_name(project, "clickhouse"),
         "clickhouse-client", "--user", "clickhouse", "--password", password, "-q", sql],
        capture_output=True, text=True, timeout=timeout,
    )
    return p.returncode == 0, (p.stdout or p.stderr).strip()


def wait_events_table(project: str, password: str, timeout: float = 300,
                      interval: float = 5) -> tuple[bool, str]:
    """等 langfuse-web 的迁移把 events_full 建出来。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        ok, out = clickhouse_query(
            project,
            "SELECT count() FROM system.tables "
            "WHERE database = 'default' AND name = 'events_full'",
            password,
        )
        if ok and out.strip() not in ("", "0"):
            return True, "events_full 已建出"
        last = out or "(无输出)"
        time.sleep(interval)
    return False, f"等待超时（{int(timeout)}s），events_full 仍未出现：{last}"


def published_ports(project: str) -> set[int]:
    """本项目正在占用的主机端口。

    分配器需要区分「被别的进程占用」和「被我们自己的容器占用」：
    后者在重复执行 init 时必须继续复用，否则会把已经起好的栈换到另一套端口，
    留下一批孤儿容器。
    """
    p = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
         "--format", "{{.Ports}}"],
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        return set()

    ports: set[int] = set()
    for segment in p.stdout.replace(",", " ").split():
        if "->" not in segment:
            continue
        host = segment.split("->", 1)[0]
        _, _, port = host.rpartition(":")
        if port.isdigit():
            ports.add(int(port))
    return ports


def wait_web_health(port: int, timeout: float = 180, interval: float = 3) -> tuple[bool, str]:
    """Langfuse web 启动后还要跑完一次性初始化，健康端点会晚于容器 running 就绪。"""
    from harborstack.env import probe

    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if probe.http_ok(f"http://127.0.0.1:{port}/api/public/health", timeout=3):
            return True, f"/api/public/health 200（端口 {port}）"
        last = f"/api/public/health 不可达（端口 {port}）"
        time.sleep(interval)
    return False, f"等待超时（{int(timeout)}s）：{last}"


def gate(cfg: dict, env_file, password: str, timeout: float = 600,
         patched: bool = True) -> list[dict]:
    """返回 [{"id","status","detail"}]，供 init 汇总。"""
    project = render.project_name(cfg)
    cmd = render.compose_cmd(cfg, env_file, patched=patched)
    checks: list[dict] = []

    ok, detail = wait_containers(cmd, timeout=timeout)
    checks.append({"id": "containers", "status": "ok" if ok else "fail", "detail": detail})

    ok, detail = clickhouse_query(project, "SELECT 1", password)
    checks.append({"id": "clickhouse", "status": "ok" if ok else "fail",
                   "detail": "SELECT 1 通过" if ok else detail})

    web_port = _web_port(env_file)
    ok, detail = wait_web_health(web_port)
    checks.append({"id": "web", "status": "ok" if ok else "fail", "detail": detail})

    if checks[1]["status"] == "ok":
        ok, detail = wait_events_table(project, password)
    else:
        ok, detail = False, "ClickHouse 不可用，跳过表检查"
    checks.append({"id": "events_full", "status": "ok" if ok else "fail", "detail": detail})
    return checks


def _web_port(env_file) -> int:
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("HS_PORT_WEB="):
            return int(line.split("=", 1)[1])
    raise ValueError(f"{env_file} 缺少 HS_PORT_WEB")
