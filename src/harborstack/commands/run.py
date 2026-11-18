"""run：透传本地 harbor 仓的 CLI，产物落在 var/runs/<id>。

边界（design §6 / ADR-3）：

- 不 clone、不 fetch、不切分支、不装依赖、不冻结版本
- 只使用该仓**已有**的可执行环境；找不到就拒绝执行，并给出准备环境的提示
- cwd 保持调用方目录，`-c config.yaml` 这类相对路径照常有效；
  产物通过自动补上的 `-o/--out` 落到 run 目录，不写入该仓
- HEAD / 分支 / 脏状态只记入 run.json 供追溯，不做校验、不阻塞
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

from harborstack import config as cfgmod

RUN_META = "run.json"
SCHEMA = 1

# harbor 自身 0/2 原样传递，其余一律归为「数据/运行失败」
_PASSTHROUGH = {0, 2}
EXIT_DATA = 5


class RunError(Exception):
    """无法执行 harbor，或参数不合法。"""


# ---------- 命名 ----------

def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def resolve_run_dir(cfg: dict, out: str | None) -> Path:
    if out:
        return cfgmod.expand_path(out)
    return cfgmod.expand_path(cfg["paths"]["var_dir"]) / "runs" / new_run_id()


# ---------- harbor 环境发现 ----------

def git_info(repo: Path) -> dict:
    def call(*args: str) -> str:
        try:
            p = subprocess.run(["git", "-C", str(repo), *args],
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return ""
        return p.stdout.strip() if p.returncode == 0 else ""

    return {
        "head": call("rev-parse", "HEAD") or None,
        "branch": call("rev-parse", "--abbrev-ref", "HEAD") or None,
        "dirty": bool(call("status", "--porcelain")),
    }


def harbor_prefix(repo: Path) -> tuple[list[str], str]:
    """返回 (命令前缀, runner 名)。只接受已存在的环境，绝不触发安装。"""
    candidates = [(repo / ".venv" / "bin" / "harbor", "repo-venv")]
    uv_env = os.environ.get("UV_PROJECT_ENVIRONMENT")
    if uv_env:
        candidates.append((cfgmod.expand_path(uv_env) / "bin" / "harbor", "uv-env"))
    for path, runner in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return [str(path)], runner

    found = shutil.which("harbor")
    if found:
        return [found], "global"

    raise RunError(
        f"{repo} 没有可用的 harbor 环境。请先在该仓准备依赖（例如 "
        f"`cd {repo} && uv sync`），或把 harbor 装进 PATH；"
        "harborstack 不负责安装依赖")


# ---------- 参数拼装 ----------

def jobs_dir_flag(args: list[str]) -> tuple[bool, str | None]:
    """检测用户是否自己指定了输出目录。"""
    for i, arg in enumerate(args):
        if arg in ("-o", "--jobs-dir"):
            return True, args[i + 1] if i + 1 < len(args) else None
        if arg.startswith("--jobs-dir="):
            return True, arg.split("=", 1)[1]
    return False, None


def build_command(prefix: list[str], harbor_args: list[str],
                  run_dir: Path) -> tuple[list[str], Path]:
    """用户没指定 -o 时注入 run_dir；指定了就以用户的为准。"""
    has_jobs_dir, value = jobs_dir_flag(harbor_args)
    args = list(harbor_args)
    if has_jobs_dir:
        jobs_dir = cfgmod.expand_path(value) if value else run_dir
    else:
        jobs_dir = run_dir
        args += ["-o", str(run_dir)]
    return prefix + args, jobs_dir


# ---------- 主流程 ----------

def _save_meta(run_dir: Path, meta: dict) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / RUN_META
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
    return path


def run(cfg: dict, harbor_args: list[str], out: str | None = None,
        dry_run: bool = False, log=print) -> dict:
    if not harbor_args:
        raise RunError("缺少 harbor 参数，用法：harborstack run [选项] -- <harbor 参数…>")

    repo = cfgmod.expand_path(cfg["paths"]["harbor_repo"])
    if not repo.is_dir():
        raise RunError(
            f"harbor 工作区不存在：{repo}（检查 paths.harbor_repo，"
            f"可临时用 --set paths.harbor_repo=/path/to/harbor 指定）")
    if not (repo / "pyproject.toml").is_file():
        raise RunError(f"{repo} 不像 harbor 源码仓（缺少 pyproject.toml）")

    run_dir = resolve_run_dir(cfg, out)
    prefix, runner = harbor_prefix(repo)
    cmd, jobs_dir = build_command(prefix, harbor_args, run_dir)

    meta = {
        "schema": SCHEMA,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "harbor": {"repo": str(repo), "runner": runner, **git_info(repo)},
        "argv": list(harbor_args),
        "command": cmd,
        "jobs_dir": str(jobs_dir),
        "run_dir": str(run_dir),
        "exit_code": None,
        "mapped_exit": None,
    }

    if dry_run:
        log("将执行：" + " ".join(cmd))
        log(f"run 目录：{run_dir}")
        return {"ok": True, "dry_run": True, "run_dir": str(run_dir),
                "jobs_dir": str(jobs_dir), "command": cmd,
                "harbor": meta["harbor"], "exit_code": None, "mapped_exit": None}

    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        rc = subprocess.call(cmd, cwd=str(Path.cwd()))
    except KeyboardInterrupt:
        rc = 130
    except OSError as exc:
        raise RunError(f"无法启动 harbor：{exc}") from exc

    meta["exit_code"] = rc
    meta["mapped_exit"] = rc if rc in _PASSTHROUGH else EXIT_DATA
    meta["elapsed_s"] = round(time.time() - started, 3)
    meta["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _save_meta(run_dir, meta)

    return {"ok": rc == 0, "dry_run": False, "run_dir": str(run_dir),
            "jobs_dir": str(jobs_dir), "command": cmd, "harbor": meta["harbor"],
            "exit_code": rc, "mapped_exit": meta["mapped_exit"],
            "elapsed_s": meta["elapsed_s"]}


def print_human(result: dict) -> None:
    harbor = result.get("harbor") or {}
    head = (harbor.get("head") or "")[:10]
    branch = harbor.get("branch") or ""
    dirty = " 有未提交改动" if harbor.get("dirty") else ""
    print(f"harbor     {harbor.get('repo', '')}")
    if head:
        print(f"版本       {head} ({branch}){dirty}  [{harbor.get('runner', '')}]")
    print(f"命令       {' '.join(result.get('command') or [])}")
    print(f"run 目录   {result.get('run_dir', '')}")
    print(f"jobs 目录  {result.get('jobs_dir', '')}")
    if result.get("dry_run"):
        print("模式       dry-run（未执行）")
        return
    rc = result.get("exit_code")
    mark = "ok  " if result.get("ok") else "FAIL"
    print(f"[{mark}] harbor 退出码 {rc}"
          f"（harborstack 映射为 {result.get('mapped_exit')}）")
    if result.get("ok"):
        print(f"\n下一步     harborstack display {result.get('run_dir')}")
