"""install：把镜像与补丁产物准备到本地。可重复执行，已就绪的步骤跳过。"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from harborstack import config as cfgmod
from harborstack import fsutil
from harborstack import lockfile
from harborstack.env import docker, probe
from harborstack.patch import pipeline


class InstallError(Exception):
    pass


@dataclass
class Action:
    id: str
    status: str      # ok | skip | plan | fail
    detail: str


def _tag(source: str, target: str) -> None:
    """按 digest 拉取不会创建 tag；补上 tag，compose 才不会按 tag 重新解析到别的镜像。"""
    p = subprocess.run(["docker", "tag", source, target],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise InstallError(
            f"标记镜像失败：{source} -> {target}\n{p.stderr.strip()[:300]}")


PULL_TIMEOUT = 1800
MIRROR_HINT = ("代理可能未缓存该 digest 或链路卡住；可换源重试："
               "HARBORSTACK_IMAGE__MIRROR=docker.m.daocloud.io harborstack install")


def _run_pull(cmd: list[str]) -> subprocess.CompletedProcess | None:
    """执行一次 pull；超时返回 None，交给调用方决定是否换路径重试。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=PULL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None


def _pull(ref: str, digest: str) -> Action:
    """优先按 digest 拉取并把 tag 钉到该 digest，失败或超时再按 tag 拉取并核对 digest。"""
    target = f"{ref}@{digest}"
    p = _run_pull(["docker", "pull", target])
    if p is not None and p.returncode == 0:
        _tag(target, ref)
        return Action(ref, "ok", f"已按 digest 拉取并标记 {target}")

    # digest 路径失败或超时都退回 tag 路径：两者是 registry 上两条不同的解析路径，
    # 卡在其中一条时另一条仍可能成功；拿到镜像后照旧用 lock 的 digest 校验。
    p2 = _run_pull(["docker", "pull", ref])
    if p2 is None:
        raise InstallError(
            f"{ref} 拉取超时（各 {PULL_TIMEOUT}s）：digest 与 tag 两条路径均未完成\n"
            f"{MIRROR_HINT}")
    if p2.returncode != 0:
        raise InstallError(
            f"拉取失败：{target}\n{p2.stderr.strip()[:400]}\n{MIRROR_HINT}")

    actual = docker.image_digest(ref)
    if actual != digest:
        raise InstallError(
            f"{ref} 拉到的 digest 与 lock 不符：{actual} != {digest}\n"
            "tag 已漂移；请运行 harborstack upgrade 重新冻结，不要继续使用旧 lock")
    return Action(ref, "ok", "按 tag 拉取后 digest 校验通过")


def ensure_images(cfg: dict, lock: lockfile.Lock, dry_run: bool) -> list[Action]:
    actions: list[Action] = []
    for name, pin in lock.pins.items():
        ref = cfgmod.image_ref(cfg, name)
        actual = docker.image_digest(ref)
        if actual == pin.digest:
            actions.append(Action(name, "skip", f"已就绪 {pin.digest[:19]}…"))
        elif dry_run:
            actions.append(Action(name, "plan", f"将拉取 {ref}@{pin.digest[:19]}…"))
        else:
            try:
                actions.append(_pull(ref, pin.digest))
            except InstallError as exc:
                # 单个镜像失败不中断整批：继续尝试其余镜像，最后统一汇总
                actions.append(Action(name, "fail", str(exc)))
    return actions


def import_bundle(cfg: dict, lock: lockfile.Lock, bundle_dir: Path,
                  log=print) -> list[Action]:
    """从离线包导入镜像与穿刺二进制，全程不访问外网。"""
    from harborstack.commands import bundle as bundle_cmd

    bundle_dir = Path(bundle_dir).expanduser().resolve()
    actions: list[Action] = []

    verdict = bundle_cmd.verify(bundle_dir)
    if not verdict["ok"]:
        raise InstallError(
            f"bundle 校验失败：缺失 {verdict['missing']}，被改动 {verdict['mismatch']}")
    actions.append(Action("bundle", "ok", f"{bundle_dir}（{verdict['mode']}）校验通过"))

    tar = bundle_dir / "images.tar"
    if tar.exists():
        rc, out = docker.run(["docker", "load", "-i", str(tar)], timeout=7200)
        if rc != 0:
            raise InstallError(f"docker load 失败：{out[:400]}")
        actions.append(Action("images", "ok", f"已从 {tar.name} 导入"))

    src = bundle_dir / "patch" / f"{lock.patched_binary_stem()}.bin"
    if src.exists():
        dest = cfgmod.expand_path(cfg["paths"]["cache_dir"]) / "bins" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(src.read_bytes())
        tmp.chmod(0o755)
        tmp.replace(dest)
        expected = lock.artifacts["clickhouse_patch"]["output_sha256"]
        actual = fsutil.sha256_file(dest)
        if actual != expected:
            raise InstallError(f"bundle 里的补丁产物 sha256 不符：{actual} != {expected}")
        actions.append(Action("clickhouse_patch", "ok", f"已导入 {dest.name}"))

    return actions


def run_install(cfg: dict, lock: lockfile.Lock, dry_run: bool = False,
                from_bundle: Path | None = None, log=print) -> dict:
    ok, info = docker.daemon_ok()
    if not ok:
        raise InstallError(f"docker 不可用：{info}")

    actions: list[Action] = []
    if from_bundle is not None:
        if dry_run:
            actions.append(Action("bundle", "plan", f"将从 {from_bundle} 导入镜像与补丁产物"))
        else:
            actions.extend(import_bundle(cfg, lock, from_bundle, log=log))

    actions.extend(ensure_images(cfg, lock, dry_run))

    patch_info = None
    if probe.needs_clickhouse_patch():
        if not cfg["platform"]["allow_patch"]:
            raise InstallError("本机需要穿刺，但 platform.allow_patch=false")
        if dry_run:
            cached = pipeline.verify_cached(cfg, lock)
            if cached:
                actions.append(Action("clickhouse_patch", "skip",
                                      f"缓存命中 {cached.path.name}"))
            else:
                actions.append(Action("clickhouse_patch", "plan",
                                      "将执行 LDAPR* -> LDAR* 流水线"))
        else:
            result = pipeline.ensure_patched(cfg, lock, log=log)
            patch_info = {
                "path": str(result.path),
                "sha256": result.output_sha256,
                "cached": result.cached,
                "sites": result.sites,
            }
            actions.append(Action("clickhouse_patch",
                                  "skip" if result.cached else "ok",
                                  f"{'缓存命中' if result.cached else '已生成'} {result.path.name}"))
    else:
        actions.append(Action("clickhouse_patch", "skip", "本机无需穿刺"))

    failures = [a for a in actions if a.status == "fail"]
    return {
        "dry_run": dry_run,
        "actions": [asdict(a) for a in actions],
        "patch": patch_info,
        "ok": not failures,
    }


def print_human(result: dict) -> None:
    marks = {"ok": "ok  ", "skip": "skip", "plan": "plan", "fail": "FAIL"}
    width = max(len(a["id"]) for a in result["actions"])
    for a in result["actions"]:
        print(f"[{marks[a['status']]}] {a['id']:<{width}}  {a['detail']}")
    if result.get("patch"):
        p = result["patch"]
        print(f"\n补丁产物：{p['path']}\n  sha256 {p['sha256']}")
        sites = {k: v for k, v in p["sites"].items() if k != "total"}
        print(f"  命中 {p['sites'].get('total')} 处（{sites}）")
