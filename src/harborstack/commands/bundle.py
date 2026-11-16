"""bundle：把一次 init 需要的全部外部输入打成可搬运目录。

三档不是「大小不同」，而是「目标机器缺什么」不同：

- skeleton：目标机器已有六个镜像，只缺拓扑、锁、穿刺产物
- full：目标机器没有任何镜像，用 docker save 的 images.tar 一次性导入
- offline：目标机器连 PyPI 都没有，额外带上 harborstack 自己的 wheel

git 里只存本文件与期望指纹，images.tar 与二进制永远不进仓库。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from harborstack import config as cfgmod
from harborstack import fsutil
from harborstack import lockfile
from harborstack.deploy import all_assets

MODES = ("skeleton", "full", "offline")
MANIFEST = "manifest.json"

MODE_DESC = {
    "skeleton": "拓扑 + 锁 + 穿刺产物（假定目标机器已有镜像）",
    "full": "skeleton + images.tar（docker save 六个镜像）",
    "offline": "full + wheels/（harborstack 及其依赖，目标机器无需 PyPI）",
}


class BundleError(Exception):
    pass


def _ensure_pip(log=print) -> None:
    """offline 档需要 pip；uv 创建的 venv 默认没有，先用 ensurepip 补齐。"""
    probe = subprocess.run([sys.executable, "-m", "pip", "--version"],
                           capture_output=True, text=True)
    if probe.returncode == 0:
        return
    fix = subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"],
                         capture_output=True, text=True)
    if fix.returncode != 0:
        raise BundleError(
            "当前 Python 没有 pip，且 ensurepip 不可用；"
            "请先安装 pip（uv 环境可运行 `uv pip install pip`）")
    log("[info] 已通过 ensurepip 补齐 pip")


def _files_manifest(root: Path) -> list[dict]:
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == MANIFEST:
            continue
        rows.append({
            "path": str(path.relative_to(root)),
            "size": path.stat().st_size,
            "sha256": fsutil.sha256_file(path),
        })
    return rows


def create(cfg: dict, lock: lockfile.Lock, out: Path, mode: str = "full",
           log=print) -> dict:
    if mode not in MODES:
        raise BundleError(f"mode 只能是 {'/'.join(MODES)}，当前：{mode}")

    out = Path(out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    compose_dir = out / "compose"
    compose_dir.mkdir(exist_ok=True)
    for asset in all_assets():
        shutil.copy2(asset, compose_dir / asset.name)

    lock_src = lock.path
    if lock_src and Path(lock_src).exists():
        shutil.copy2(lock_src, out / "lock.json")

    binary = None
    if cfg["bundle"].get("include_patched_bin", True):
        cache = cfgmod.expand_path(cfg["paths"]["cache_dir"]) / "bins"
        target = cache / f"{lock.patched_binary_stem()}.bin"
        if target.exists():
            patch_dir = out / "patch"
            patch_dir.mkdir(exist_ok=True)
            dest = patch_dir / target.name
            shutil.copy2(target, dest)
            binary = {"path": f"patch/{dest.name}", "sha256": fsutil.sha256_file(dest)}
        else:
            log(f"[warn] 未找到补丁产物 {target}；先运行 harborstack install")

    images: list[dict] = []
    if mode in ("full", "offline"):
        refs = [cfgmod.image_ref(cfg, name) for name in lock.pins]
        tar = out / "images.tar"
        log(f"[info] docker save {len(refs)} 个镜像 -> {tar}")
        p = subprocess.run(["docker", "save", "-o", str(tar), *refs],
                           capture_output=True, text=True, timeout=7200)
        if p.returncode != 0:
            raise BundleError(f"docker save 失败：{p.stderr.strip()[:600]}")
        images = [{"name": name, "ref": cfgmod.image_ref(cfg, name),
                   "digest": lock.pins[name].digest} for name in lock.pins]

    wheels: list[str] = []
    if mode == "offline":
        wheels_dir = out / "wheels"
        wheels_dir.mkdir(exist_ok=True)
        repo = Path.cwd()
        if not (repo / "pyproject.toml").exists():
            raise BundleError("offline 模式需要在仓库根执行（找不到 pyproject.toml）")
        _ensure_pip(log)
        for cmd in (
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(wheels_dir), str(repo)],
            [sys.executable, "-m", "pip", "download", "tomli", "-d", str(wheels_dir)],
        ):
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if p.returncode != 0:
                raise BundleError(f"{' '.join(cmd[:4])} 失败：{p.stderr.strip()[:400]}")
        wheels = [f"wheels/{p.name}" for p in sorted(wheels_dir.glob("*.whl"))]

    files = _files_manifest(out)
    data = {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode,
        "mode_desc": MODE_DESC[mode],
        "project": cfg.get("stack", {}).get("env", "default"),
        "images": images,
        "binary": binary,
        "wheels": wheels,
        "total_bytes": sum(f["size"] for f in files),
        "files": files,
    }
    manifest_path = out / MANIFEST
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return {"ok": True, "dir": str(out), "mode": mode,
            "total_bytes": data["total_bytes"], "files": len(files),
            "manifest": str(manifest_path)}


def verify(out: Path) -> dict:
    out = Path(out).expanduser().resolve()
    man_path = out / MANIFEST
    if not man_path.exists():
        raise BundleError(f"{man_path} 不存在，这不是一个完整 bundle")
    try:
        data = json.loads(man_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BundleError(f"{MANIFEST} 不是合法 JSON：{e}") from e

    missing, mismatch = [], []
    for row in data.get("files", []):
        path = out / row["path"]
        if not path.exists():
            missing.append(row["path"])
            continue
        if path.stat().st_size != row["size"] or fsutil.sha256_file(path) != row["sha256"]:
            mismatch.append(row["path"])

    ok = not missing and not mismatch
    return {"ok": ok, "dir": str(out), "mode": data.get("mode"),
            "files": len(data.get("files", [])), "missing": missing,
            "mismatch": mismatch}


def print_create(result: dict) -> None:
    print(f"[ok  ] bundle {result['mode']} -> {result['dir']}")
    print(f"  文件 {result['files']} 个，合计 {result['total_bytes'] / (1 << 30):.2f} GB")
    print(f"  清单 {result['manifest']}")


def print_verify(result: dict) -> None:
    mark = "ok  " if result["ok"] else "FAIL"
    print(f"[{mark}] bundle {result['dir']}  mode={result['mode']}  文件 {result['files']} 个")
    for p in result["missing"]:
        print(f"  缺失 {p}")
    for p in result["mismatch"]:
        print(f"  被改动 {p}")
