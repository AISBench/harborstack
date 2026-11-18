"""down：只清理本 compose 项目，不触碰机器上其他人的任何容器或卷。

安全约束（共享机器红线）：
- 只按 compose 项目名操作，绝不 docker system prune / volume prune
- 项目名必须以 harborstack- 开头，防止配置写错时误删别人的栈
"""

from __future__ import annotations

from harborstack import config as cfgmod
from harborstack import lockfile
from harborstack import manifest
from harborstack.deploy import render
from harborstack.env import docker, probe

PREFIX = "harborstack-"


class DownError(Exception):
    pass


def run_down(cfg: dict, lock: lockfile.Lock, dry_run: bool = False,
             remove_volumes: bool = True, log=print) -> dict:
    project = render.project_name(cfg)
    if not project.startswith(PREFIX) or len(project) == len(PREFIX):
        raise DownError(f"拒绝执行：项目名 {project!r} 不符合 {PREFIX}<env> 形式")

    var_dir = cfgmod.expand_path(cfg["paths"]["var_dir"])
    env_file = var_dir / render.ENV_NAME
    if not env_file.exists():
        return {"ok": True, "project": project, "dry_run": dry_run,
                "detail": f"{env_file} 不存在，本机没有由 harborstack 启动的栈"}

    args = ["down", "--remove-orphans"]
    if remove_volumes:
        args.append("--volumes")

    if dry_run:
        return {"ok": True, "project": project, "dry_run": True,
                "detail": "将执行 docker compose " + " ".join(args)}

    patched = probe.needs_clickhouse_patch()
    rc, out = docker.run(render.compose_cmd(cfg, env_file, *args, patched=patched),
                         timeout=600)
    ok = rc == 0

    man_path = manifest.path_for(cfg)
    if ok and man_path.exists():
        man_path.unlink()

    return {"ok": ok, "project": project, "dry_run": False,
            "volumes_removed": remove_volumes,
            "detail": out[:600] if out else "已清理"}


def print_human(result: dict) -> None:
    if result["ok"]:
        print(f"[ok  ] {result['project']}  {result['detail']}")
    else:
        print(f"[FAIL] {result['project']}  {result['detail']}")
