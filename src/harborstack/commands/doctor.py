"""doctor：只诊断、不修改。退出码 0 = 无 fail，3 = 存在 fail。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path

from harborstack import config as cfgmod
from harborstack import lockfile
from harborstack.env import docker, probe

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    id: str
    status: str
    message: str
    hint: str = ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_platform(cfg: dict) -> Check:
    machine = probe.machine()
    rcpc = probe.has_rcpc()
    needs = probe.needs_clickhouse_patch()
    if needs and not cfg["platform"]["allow_patch"]:
        return Check("platform", FAIL,
                     f"{machine} 且 CPU 无 rcpc，需要穿刺但 platform.allow_patch=false",
                     "在 harborstack.toml 设置 platform.allow_patch = true，或换支持 RCpc 的机器")
    detail = f"{machine} | rcpc={'yes' if rcpc else 'no'} | needs_patch={'yes' if needs else 'no'}"
    if machine not in ("aarch64", "x86_64"):
        return Check("platform", WARN, detail, "未验证的架构，ClickHouse 镜像可能不可用")
    return Check("platform", OK, detail)


def _check_patch_artifact(cfg: dict, lock: lockfile.Lock) -> Check:
    patch = lock.artifacts.get("clickhouse_patch")
    if not probe.needs_clickhouse_patch():
        return Check("patch", OK, "本机无需穿刺，跳过补丁校验")
    if not patch:
        return Check("patch", FAIL, "lock.json 缺少 artifacts.clickhouse_patch",
                     "补跑补丁流水线后写回 lock.json")

    cache_dir = cfgmod.expand_path(cfg["paths"]["cache_dir"])
    target = cache_dir / "bins" / f"{lock.patched_binary_stem()}.bin"
    if not target.exists():
        return Check("patch", WARN, f"补丁产物未缓存：{target}",
                     "运行 harborstack install 生成")

    actual = _sha256(target)
    expected = patch["output_sha256"]
    if actual != expected:
        return Check("patch", FAIL, f"补丁产物 sha256 不符：{actual[:16]}… != {expected[:16]}…",
                     "删除缓存产物后重跑补丁流水线，不要复用旧字节")
    return Check("patch", OK, f"补丁产物校验通过（sha256 {actual[:12]}…）")


def _check_assets(lock: lockfile.Lock) -> Check:
    """compose 资产指纹：vendored 快照被手改、或被上游悄悄替换，都要立刻发现。"""
    from harborstack.deploy import all_assets

    if not lock.compose:
        return Check("assets", WARN, "lock.json 未记录 compose 资产指纹",
                     "重新冻结后写回 lock.json 的 compose.files")

    missing, mismatch = [], []
    for path in all_assets():
        expected = lock.compose.get(path.name)
        if not path.exists():
            missing.append(path.name)
        elif expected is None:
            mismatch.append(f"{path.name}(无锁定值)")
        elif _sha256(path) != expected:
            mismatch.append(path.name)

    if missing:
        return Check("assets", FAIL, f"compose 资产缺失：{', '.join(missing)}",
                     "重新安装 harborstack，或从 bundle 的 compose/ 目录恢复")
    if mismatch:
        return Check("assets", FAIL, f"compose 资产与 lock 不符：{', '.join(mismatch)}",
                     "不要直接改这些文件；改动必须同步更新 lock.json 并复验")
    return Check("assets", OK, f"{len(lock.compose)} 份 compose 资产指纹一致")


def _check_manifest(cfg: dict) -> Check:
    from harborstack import manifest

    man = manifest.load(manifest.path_for(cfg))
    if man is None:
        return Check("manifest", WARN, "尚未 init（没有 env-manifest.json）",
                     "运行 harborstack init 完成部署")
    state = man.get("state")
    if state == "ready":
        return Check("manifest", OK, f"state=ready，端口 {man.get('ports', {})}")
    return Check("manifest", WARN, f"state={state}",
                 "上一次 init 未走完；重新执行 harborstack init 会补齐")


def _check_docker() -> list[Check]:
    ok, info = docker.daemon_ok()
    checks = [Check("docker", OK if ok else FAIL,
                    f"docker server {info}" if ok else f"docker 不可用：{info}",
                    "" if ok else "确认当前用户在 docker 组，且 daemon 正在运行")]
    if ok:
        cok, cinfo = docker.compose_ok()
        checks.append(Check("compose", OK if cok else FAIL,
                            f"docker compose {cinfo}" if cok else f"docker compose 不可用：{cinfo}",
                            "" if cok else "安装 docker compose v2 插件"))
    return checks


def _check_images(cfg: dict, lock: lockfile.Lock) -> Check:
    missing, mismatch, present = [], [], 0
    for name, pin in lock.pins.items():
        ref = cfgmod.image_ref(cfg, name)
        actual = docker.image_digest(ref)
        if actual is None:
            missing.append(name)
        elif actual != pin.digest:
            mismatch.append(f"{name}: 本地 {actual[:19]}… ≠ 锁定 {pin.digest[:19]}…")
        else:
            present += 1

    if mismatch:
        return Check("images", FAIL, "镜像 digest 与 lock 不符：" + "; ".join(mismatch),
                     "不要直接 pull 覆盖；先重新冻结 lock.json，再执行 harborstack install")
    if missing:
        return Check("images", WARN, f"{present}/{len(lock.pins)} 就绪，缺失：{', '.join(missing)}",
                     "运行 harborstack install 拉取（或从 bundle 导入）")
    return Check("images", OK, f"{present}/{len(lock.pins)} 镜像 digest 全部匹配")


def _check_ports(cfg: dict) -> Check:
    web = cfg["ports"]["preferred"]["web"]
    if probe.port_free(web):
        return Check("ports", OK, f"{web} 可用（127.0.0.1）")
    if probe.http_ok(f"http://127.0.0.1:{web}/api/public/health"):
        return Check("ports", OK, f"{web} 已被一个健康的 Langfuse 占用，init 会自动改选其他端口")
    return Check("ports", WARN, f"{web} 被其他进程占用",
                 "init 时端口分配器会自动改选，或调整 ports.preferred.web")


def _check_paths(cfg: dict) -> list[Check]:
    checks: list[Check] = []

    harbor = cfgmod.expand_path(cfg["paths"]["harbor_repo"])
    if not harbor.exists():
        checks.append(Check("harbor_repo", WARN, f"harbor 仓不存在：{harbor}",
                            "设置 paths.harbor_repo，run 子命令依赖它"))
    elif not (harbor / "pyproject.toml").exists():
        checks.append(Check("harbor_repo", WARN, f"{harbor} 不像 harbor 仓（无 pyproject.toml）",
                            "确认路径指向 harbor 代码仓根目录"))
    else:
        checks.append(Check("harbor_repo", OK, str(harbor)))

    var_dir = cfgmod.expand_path(cfg["paths"]["var_dir"])
    if probe.is_writable_dir(var_dir):
        checks.append(Check("var_dir", OK, f"{var_dir} 可写"))
    else:
        checks.append(Check("var_dir", FAIL, f"{var_dir} 不可写", "调整 paths.var_dir"))

    free = probe.disk_free(var_dir)
    gb = free / (1 << 30)
    if gb < 5:
        checks.append(Check("disk", FAIL, f"剩余 {gb:.1f} GB", "至少保留 5GB 用于镜像与数据卷"))
    elif gb < 15:
        checks.append(Check("disk", WARN, f"剩余 {gb:.1f} GB", "整套镜像约 5.5GB，建议留 15GB 以上"))
    else:
        checks.append(Check("disk", OK, f"剩余 {gb:.1f} GB"))
    return checks


def run_doctor(cfg: dict, lock: lockfile.Lock) -> list[Check]:
    checks: list[Check] = []
    checks.append(_check_platform(cfg))
    checks.extend(_check_docker())
    checks.append(_check_images(cfg, lock))
    checks.append(_check_patch_artifact(cfg, lock))
    checks.append(_check_assets(lock))
    checks.append(_check_manifest(cfg))
    checks.append(_check_ports(cfg))
    checks.extend(_check_paths(cfg))
    return checks


def report(checks: list[Check]) -> dict:
    summary = {OK: 0, WARN: 0, FAIL: 0}
    for c in checks:
        summary[c.status] += 1
    return {
        "checks": [asdict(c) for c in checks],
        "summary": summary,
        "ok": summary[FAIL] == 0,
    }


def print_human(checks: list[Check]) -> None:
    marks = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    width = max(len(c.id) for c in checks) if checks else 4
    for c in checks:
        print(f"[{marks[c.status]}] {c.id:<{width}}  {c.message}")
        if c.hint and c.status != OK:
            print(f"{'':<{width + 9}}→ {c.hint}")
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    print(f"\n结果：{len(checks)} 项检查，fail={fails} warn={warns}")
