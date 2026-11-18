"""宿主机探测：平台、CPU 特性、磁盘、端口、HTTP。只读，不修改环境。"""

from __future__ import annotations

import platform
import shutil
import socket
import urllib.request
from pathlib import Path


def machine() -> str:
    return platform.machine()


def cpu_flags() -> set[str]:
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    for line in text.splitlines():
        if line.lower().startswith(("flags", "features")):
            _, _, value = line.partition(":")
            return set(value.split())
    return set()


def has_rcpc() -> bool:
    return "rcpc" in cpu_flags()


def needs_clickhouse_patch() -> bool:
    """官方 arm64 构建要求 ARMv8.2-A 且支持 Load-Acquire RCpc。"""
    return machine() == "aarch64" and not has_rcpc()


def disk_free(path: Path) -> int:
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    return shutil.disk_usage(target).free


def is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".harborstack-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False
