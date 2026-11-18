"""补丁流水线：镜像 -> 提取 -> 穿刺 -> 冒烟 -> 缓存。

缓存键 = (ClickHouse 镜像 digest 前缀, patcher 版本)，见 lockfile.Lock.patched_binary_stem。
命中条件不是"文件存在"，而是"文件 sha256 与 lock 记录一致"。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from harborstack import config as cfgmod
from harborstack import lockfile
from harborstack.patch import elf, rcpc

PATCHER_VERSION = "1.0.0"
SMOKE_TIMEOUT = 300


class PipelineError(Exception):
    """流水线失败，消息必须能指导下一步动作。"""


@dataclass
class PatchResult:
    path: Path
    source_sha256: str
    output_sha256: str
    sites: dict
    cached: bool


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(args: list[str], timeout: int = 900) -> str:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise PipelineError(f"命令失败（{p.returncode}）：{' '.join(args)}\n{p.stderr.strip()[:800]}")
    return p.stdout.strip()


def cache_paths(cfg: dict, lock: lockfile.Lock) -> tuple[Path, Path]:
    stem = lock.patched_binary_stem()
    base = cfgmod.expand_path(cfg["paths"]["cache_dir"]) / "bins"
    return base / f"{stem}.bin", base / f"{stem}.json"


def verify_cached(cfg: dict, lock: lockfile.Lock) -> PatchResult | None:
    """缓存命中返回结果，否则 None（文件损坏也算 miss）。"""
    bin_path, meta_path = cache_paths(cfg, lock)
    if not bin_path.exists():
        return None
    expected = lock.artifacts["clickhouse_patch"]["output_sha256"]
    actual = sha256_file(bin_path)
    if actual != expected:
        return None
    sites = {}
    if meta_path.exists():
        try:
            sites = json.loads(meta_path.read_text(encoding="utf-8")).get("sites", {})
        except json.JSONDecodeError:
            sites = {}
    return PatchResult(path=bin_path, source_sha256=lock.artifacts["clickhouse_patch"]["source_sha256"],
                       output_sha256=actual, sites=sites, cached=True)


def _extract_binary(image: str, dest: Path) -> None:
    name = f"harborstack-extract-{os.getpid()}-{int(time.time())}"
    try:
        _run(["docker", "create", "--name", name, image])
        _run(["docker", "cp", f"{name}:/usr/bin/clickhouse", str(dest)])
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


def _smoke_test(image: str, binary: Path) -> None:
    """同一镜像内执行，避免宿主机 glibc 差异；限制资源，不影响同机其他人。"""
    _run([
        "docker", "run", "--rm", "--network", "none", "--memory", "2g", "--cpus", "2",
        "-v", f"{binary}:/p/clickhouse:ro",
        "--entrypoint", "/bin/sh", image, "-c",
        "/p/clickhouse --version && /p/clickhouse local --query 'SELECT sum(number) FROM numbers(1000000)'",
    ], timeout=SMOKE_TIMEOUT)


def build(cfg: dict, lock: lockfile.Lock, log=print) -> PatchResult:
    """完整流水线。产物先写临时文件，校验通过后再原子替换进缓存。"""
    patch_meta = lock.artifacts.get("clickhouse_patch")
    if not patch_meta:
        raise PipelineError("lock.json 缺少 artifacts.clickhouse_patch，无法穿刺")

    image = cfgmod.image_ref(cfg, "clickhouse")
    expected_source = patch_meta["source_sha256"]
    expected_output = patch_meta["output_sha256"]

    bin_path, meta_path = cache_paths(cfg, lock)
    bin_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="harborstack-patch-") as tmp:
        tmp = Path(tmp)
        raw_path = tmp / "clickhouse.orig"
        out_path = tmp / "clickhouse"

        log(f"[1/5] 提取 {image}:/usr/bin/clickhouse")
        _extract_binary(image, raw_path)

        log("[2/5] 校验原始二进制 sha256")
        actual_source = sha256_file(raw_path)
        if actual_source != expected_source:
            raise PipelineError(
                "原始二进制与 lock 记录不符："
                f"{actual_source[:16]}… != {expected_source[:16]}…\n"
                "镜像内容已变化；确认要升级时需更新 lock.json 并重新生成补丁，"
                "不要就地覆盖")

        log("[3/5] 扫描并替换 LDAPR* -> LDAR*")
        blob = bytearray(raw_path.read_bytes())
        segments = elf.exec_segments(bytes(blob))
        stats = rcpc.apply(blob, segments)
        if stats.total == 0:
            raise PipelineError("未找到任何 LDAPR*，镜像可能已适配或 lock 指向了错误镜像")
        out_path.write_bytes(bytes(blob))
        out_path.chmod(out_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        out_path.chmod(0o755)

        residual = rcpc.scan(out_path.read_bytes(), elf.exec_segments(out_path.read_bytes()))
        if residual.total:
            raise PipelineError(f"穿刺后仍有 {residual.total} 条 LDAPR* 残留，拒绝写入缓存")

        log("[4/5] 校验穿刺后 sha256")
        actual_output = sha256_file(out_path)
        if actual_output != expected_output:
            raise PipelineError(
                "穿刺产物与 lock 记录不符："
                f"{actual_output[:16]}… != {expected_output[:16]}…\n"
                "patcher 行为已变化，确认后重写 lock.artifacts.clickhouse_patch")

        log("[5/5] 同镜像内冒烟（--version + 真实查询）")
        _smoke_test(image, out_path)

        staged = bin_path.with_suffix(".bin.tmp")
        shutil.copyfile(out_path, staged)
        staged.chmod(0o755)
        os.replace(staged, bin_path)

    site_summary = dict(stats.by_name)
    site_summary["total"] = stats.total
    meta_path.write_text(json.dumps({
        "schema": 1,
        "image": image,
        "image_digest": lock.pin("clickhouse").digest,
        "patcher": PATCHER_VERSION,
        "source_sha256": expected_source,
        "output_sha256": expected_output,
        "sites": dict(sorted(site_summary.items())),
        "first_va": hex(stats.first_va) if stats.first_va is not None else None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return PatchResult(path=bin_path, source_sha256=expected_source,
                       output_sha256=expected_output, sites=site_summary, cached=False)


def ensure_patched(cfg: dict, lock: lockfile.Lock, log=print) -> PatchResult:
    """缓存命中直接返回；否则跑流水线。"""
    hit = verify_cached(cfg, lock)
    if hit:
        log(f"[缓存] 命中 {hit.path.name}（sha256 {hit.output_sha256[:12]}…）")
        return hit
    return build(cfg, lock, log=log)
