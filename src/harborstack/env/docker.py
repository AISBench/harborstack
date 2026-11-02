"""docker / compose 的只读查询。"""

from __future__ import annotations

import json
import subprocess


def run(args: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def daemon_ok() -> tuple[bool, str]:
    rc, out = run(["docker", "version", "--format", "{{.Server.Version}}"])
    return (rc == 0, out or "docker 不可用")


def compose_ok() -> tuple[bool, str]:
    rc, out = run(["docker", "compose", "version", "--short"])
    return (rc == 0, out or "docker compose 不可用")


def project_volumes(project: str) -> list[str]:
    """列出某个 compose 项目的具名卷（按 compose 打的 project label 过滤）。"""
    rc, out = run(["docker", "volume", "ls",
                   "--filter", f"label=com.docker.compose.project={project}",
                   "--format", "{{.Name}}"])
    if rc != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def image_digest(ref: str) -> str | None:
    """返回本地镜像的 RepoDigest（形如 sha256:...），不存在返回 None。"""
    rc, out = run(["docker", "image", "inspect", ref, "--format", "{{json .RepoDigests}}"])
    if rc != 0 or not out:
        return None
    try:
        digests = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not digests:
        return None
    return digests[0].split("@", 1)[-1]


def container_running(name: str) -> bool:
    rc, out = run(["docker", "inspect", name, "--format", "{{.State.Running}}"])
    return rc == 0 and out == "true"
