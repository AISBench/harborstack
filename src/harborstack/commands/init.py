"""init：平台探测 -> 镜像与补丁 -> 端口分配 -> 渲染 -> 起栈 -> 健康门禁 -> manifest。

重复执行是安全的：已就绪的步骤跳过，端口优先复用上一次的分配结果。
任何一步不通过都不会把 manifest 标成 ready。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from harborstack import config as cfgmod
from harborstack import lockfile
from harborstack import manifest
from harborstack.commands import install as install_cmd
from harborstack.deploy import SKIP_CHECKSUM_XML, health, ports as portsmod, render
from harborstack.env import docker, probe


class InitError(Exception):
    pass


@dataclass
class Step:
    id: str
    status: str      # ok | skip | plan | fail
    detail: str


def _platform_info() -> dict:
    return {
        "machine": probe.machine(),
        "rcpc": probe.has_rcpc(),
        "needs_patch": probe.needs_clickhouse_patch(),
    }


def run_init(cfg: dict, lock: lockfile.Lock, dry_run: bool = False,
             health_timeout: float = 900, log=print) -> dict:
    var_dir = cfgmod.expand_path(cfg["paths"]["var_dir"])
    probe.is_writable_dir(var_dir)
    steps: list[Step] = []
    man_path = manifest.path_for(cfg)
    project = render.project_name(cfg)

    # 1. 平台判定
    plat = _platform_info()
    if plat["needs_patch"] and not cfg["platform"]["allow_patch"]:
        raise InitError(
            "本机 aarch64 且 CPU 无 rcpc，ClickHouse 必须穿刺，但 platform.allow_patch=false；"
            "在 harborstack.toml 打开该项，或换支持 RCpc 的机器")
    steps.append(Step("platform", "ok",
                      f"{plat['machine']} | rcpc={'yes' if plat['rcpc'] else 'no'}"
                      f" | needs_patch={'yes' if plat['needs_patch'] else 'no'}"))

    # 1.5 卷与密钥一致性：密钥重建会让卷内已初始化的数据库凭证失配
    if render.secrets_will_regenerate(cfg, var_dir):
        vols = docker.project_volumes(project)
        if vols:
            shown = "，".join(vols[:3]) + (" 等" if len(vols) > 3 else "")
            raise InitError(
                f"{project} 已存在 {len(vols)} 个数据卷（{shown}），但 "
                f"{var_dir / render.SECRETS_NAME} 缺失、损坏或 security_mode 已变更。"
                "重新生成的密码与卷内已初始化的数据库凭证不匹配，栈会起不来。\n"
                "  处理方式：恢复原来的 var/secrets.json；或确认数据可丢弃后执行 "
                "`harborstack down`（默认删卷）再 init。")
        steps.append(Step("volumes", "ok", "无历史数据卷，可安全初始化"))

    # 2. 镜像 + 补丁产物
    res = install_cmd.run_install(cfg, lock, dry_run=dry_run, log=log)
    steps += [Step(a["id"], a["status"], a["detail"]) for a in res["actions"]]
    if not res["ok"]:
        manifest.save(man_path, manifest.build(cfg, lock, plat, {}, None, manifest.FAILED))
        return {"steps": [asdict(s) for s in steps], "ok": False, "checks": []}

    patched = plat["needs_patch"]
    patch = res.get("patch")
    if patched and not dry_run and not patch:
        raise InitError("本机需要穿刺，但补丁流水线没有返回产物")
    binary = None
    if patch:
        binary = {"sha256": patch["sha256"], "patched_sites": patch["sites"]["total"],
                  "patcher": lock.artifacts["clickhouse_patch"]["patcher"]}

    # 3. 端口分配：优先复用本项目正在占用的端口
    owned = health.published_ports(project)
    is_free = lambda p: p in owned or probe.port_free(p)  # noqa: E731
    saved = render.read_ports(var_dir)
    allocated = portsmod.allocate(cfg, saved, is_free=is_free)
    reused = allocated == saved and bool(saved)
    steps.append(Step("ports", "skip" if reused else "ok",
                      "，".join(f"{k}={v}" for k, v in allocated.items())))

    # 4. 渲染 .env 与 secrets
    sec = render.load_secrets(cfg, var_dir)
    bin_path = Path(patch["path"]) if patch else None
    env = render.build_env(cfg, lock, allocated, sec, bin_path, SKIP_CHECKSUM_XML)
    env_file = render.write_env(var_dir, env)

    if dry_run:
        steps.append(Step("render", "plan", f"将写入 {env_file}"))
        steps.append(Step("up", "plan", "将启动 compose 栈并做健康门禁"))
        return {"steps": [asdict(s) for s in steps], "ok": True, "checks": [],
                "ports": allocated, "url": f"http://localhost:{allocated['web']}",
                "admin_email": cfg["langfuse"]["admin"]["email"]}

    render.write_ports(var_dir, allocated)
    steps.append(Step("render", "ok", str(env_file)))
    manifest.save(man_path, manifest.build(cfg, lock, plat, allocated, binary,
                                           manifest.PREPARED))

    # 5. compose 语法与变量校验（不启动）
    rc, out = docker.run(render.compose_cmd(cfg, env_file, "config", "-q", patched=patched),
                         timeout=120)
    if rc != 0:
        manifest.save(man_path, manifest.build(cfg, lock, plat, allocated, binary,
                                               manifest.FAILED))
        raise InitError(f"compose 渲染校验失败：{out[:800]}")
    steps.append(Step("compose", "ok", "config 校验通过"))

    # 6. 起栈
    rc, out = docker.run(render.compose_cmd(cfg, env_file, "up", "-d", "--remove-orphans",
                                            patched=patched), timeout=1800)
    if rc != 0:
        manifest.save(man_path, manifest.build(cfg, lock, plat, allocated, binary,
                                               manifest.FAILED))
        raise InitError(f"compose up 失败：{out[:800]}")
    steps.append(Step("up", "ok", f"项目 {project} 已启动"))

    # 7. 健康门禁
    checks = health.gate(cfg, env_file, sec["clickhouse_password"],
                         timeout=health_timeout, patched=patched)
    ok = all(c["status"] == "ok" for c in checks)
    man = manifest.build(cfg, lock, plat, allocated, binary,
                         manifest.READY if ok else manifest.FAILED)
    man["checks"] = checks
    manifest.save(man_path, man)

    return {
        "steps": [asdict(s) for s in steps],
        "checks": checks,
        "ok": ok,
        "ports": allocated,
        "url": f"http://localhost:{allocated['web']}",
        "manifest": str(man_path),
        "admin_email": cfg["langfuse"]["admin"]["email"],
    }


def print_human(result: dict) -> None:
    marks = {"ok": "ok  ", "skip": "skip", "plan": "plan", "fail": "FAIL"}
    rows = result.get("steps") or []
    if rows:
        width = max(len(r["id"]) for r in rows)
        for r in rows:
            print(f"[{marks[r['status']]}] {r['id']:<{width}}  {r['detail']}")

    checks = result.get("checks") or []
    if checks:
        print()
        width = max(len(c["id"]) for c in checks)
        for c in checks:
            print(f"[{marks['ok'] if c['status'] == 'ok' else marks['fail']}] "
                  f"{c['id']:<{width}}  {c['detail']}")

    if result.get("ok") and result.get("url"):
        ports = result["ports"]
        print(f"\nLangfuse   {result['url']}")
        print(f"登录账号   {result.get('admin_email', '')}")
        print("端口分配   " + "，".join(f"{k}={v}" for k, v in ports.items()))
