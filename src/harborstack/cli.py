"""harborstack CLI 入口：doctor / install / init / run / display / down / bundle / version。"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from harborstack import __version__
from harborstack import config as cfgmod
from harborstack import lockfile
from harborstack.commands import bundle as bundle_cmd
from harborstack.commands import display as display_cmd
from harborstack.commands import doctor as doctor_cmd
from harborstack.commands import down as down_cmd
from harborstack.commands import init as init_cmd
from harborstack.commands import install as install_cmd
from harborstack.commands import run as run_cmd

EXIT_OK, EXIT_USAGE, EXIT_ENV, EXIT_HEALTH, EXIT_DATA = 0, 2, 3, 4, 5

# 这两个命令只读配置，不需要 lock.json
_NO_LOCK = {"run", "display"}


def _wait_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"不是合法的秒数：{raw!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("等待秒数必须是有限数值")
    if value < 0:
        raise argparse.ArgumentTypeError("等待秒数不能为负数；不等待请用 --no-wait")
    return value


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="harborstack.toml 路径（默认当前目录）")
    p.add_argument("--lock", help="lock.json 路径（默认当前目录）")
    p.add_argument("--set", dest="sets", action="append", metavar="KEY=VALUE",
                   help="覆盖任意配置项，可重复，例如 --set ports.preferred.web=19000")
    p.add_argument("--json", action="store_true", help="输出 JSON")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harborstack", description="Harbor 评测产物到 Langfuse 的集成栈")
    p.add_argument("--version", action="version", version=f"harborstack {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="只诊断环境，不修改任何东西")
    _add_common(d)

    i = sub.add_parser("install", help="准备镜像与补丁产物（可重复执行）")
    _add_common(i)
    i.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    i.add_argument("--from-bundle", metavar="DIR", help="从离线包导入镜像与穿刺产物")

    n = sub.add_parser("init", help="分配端口、渲染 .env、起栈并通过健康门禁")
    _add_common(n)
    n.add_argument("--dry-run", action="store_true", help="只做检查与渲染计划，不起栈")
    n.add_argument("--health-timeout", type=float, default=900, help="健康门禁超时秒数")

    r = sub.add_parser("run", help="调用本地 harbor 仓 CLI，产物写到 var/runs/<id>")
    _add_common(r)
    r.add_argument("-o", "--out", help="指定 run 目录（默认 var/runs/<时间戳>-<随机>）")
    r.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")

    s = sub.add_parser("display", help="把 run 产物导入 Langfuse 并写 report.json")
    _add_common(s)
    s.add_argument("run_dir", nargs="?", help="run 目录（默认 var/runs 下最新一个）")
    s.add_argument("--dry-run", action="store_true", help="只做体检与计划，不上报")
    s.add_argument("--filter", dest="trial_filter", default="all",
                   choices=("all", "success", "failure"), help="按 reward 过滤 Trial")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--wait", dest="wait_seconds", type=_wait_seconds, metavar="SECONDS",
                   help="导入后等待 trace 可查的最长秒数（默认取配置；0 表示不等）")
    g.add_argument("--no-wait", dest="wait_seconds", action="store_const", const=0,
                   help="不等待，导入完成立即返回")

    o = sub.add_parser("down", help="只清理本项目的容器与卷")
    _add_common(o)
    o.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    o.add_argument("--keep-volumes", action="store_true", help="保留数据卷")

    b = sub.add_parser("bundle", help="生成或校验离线包")
    bsub = b.add_subparsers(dest="bundle_cmd", required=True)
    bc = bsub.add_parser("create", help="生成离线包")
    bc.add_argument("--out", required=True, help="输出目录")
    bc.add_argument("--mode", default="full", choices=bundle_cmd.MODES)
    _add_common(bc)
    bv = bsub.add_parser("verify", help="校验离线包完整性与指纹")
    bv.add_argument("dir", help="离线包目录")
    _add_common(bv)

    sub.add_parser("version", help="打印版本")
    return p


def _split_run_argv(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """run 的 harbor 参数放在 `--` 之后，避免与本命令自己的选项混淆。"""
    if not argv or argv[0] != "run":
        return argv, None
    if "--" not in argv:
        return argv, []
    idx = argv.index("--")
    return argv[:idx], argv[idx + 1:]


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    own, harbor_args = _split_run_argv(raw)
    args = _build_parser().parse_args(own)

    if args.command == "version":
        print(f"harborstack {__version__}")
        return EXIT_OK

    try:
        cfg = cfgmod.load_config(args.config, args.sets)
    except cfgmod.ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return EXIT_ENV

    lock = None
    if args.command not in _NO_LOCK:
        try:
            lock = lockfile.Lock.load(args.lock or "lock.json")
        except lockfile.LockError as e:
            print(f"lock 错误：{e}", file=sys.stderr)
            return EXIT_ENV

    if args.command == "run":
        if not harbor_args:
            print("缺少 harbor 参数，用法：harborstack run [选项] -- <harbor 参数…>",
                  file=sys.stderr)
            return EXIT_USAGE
        try:
            result = run_cmd.run(cfg, harbor_args, out=args.out, dry_run=args.dry_run)
        except run_cmd.RunError as e:
            print(f"run 拒绝执行：{e}", file=sys.stderr)
            return EXIT_ENV
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            run_cmd.print_human(result)
        return EXIT_OK if result["ok"] else result["mapped_exit"]

    if args.command == "display":
        try:
            result = display_cmd.run_display(
                cfg, run_dir=args.run_dir, dry_run=args.dry_run,
                trial_filter=args.trial_filter, wait_seconds=args.wait_seconds)
        except display_cmd.DisplayError as e:
            print(f"display 拒绝执行：{e}", file=sys.stderr)
            return EXIT_ENV
        if args.json:
            payload = result["report"] if result["report"] else result
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            display_cmd.print_human(result)
        return result["exit"]

    if args.command == "doctor":
        checks = doctor_cmd.run_doctor(cfg, lock)
        if args.json:
            print(json.dumps(doctor_cmd.report(checks), ensure_ascii=False, indent=2))
        else:
            doctor_cmd.print_human(checks)
        return EXIT_OK if all(c.status != doctor_cmd.FAIL for c in checks) else EXIT_ENV

    if args.command == "install":
        try:
            result = install_cmd.run_install(
                cfg, lock, dry_run=args.dry_run,
                from_bundle=Path(args.from_bundle) if args.from_bundle else None)
        except Exception as e:  # noqa: BLE001 - 统一转成可读错误
            print(f"install 失败：{e}", file=sys.stderr)
            return EXIT_ENV
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            install_cmd.print_human(result)
        return EXIT_OK if result["ok"] else EXIT_ENV

    if args.command == "init":
        try:
            result = init_cmd.run_init(cfg, lock, dry_run=args.dry_run,
                                       health_timeout=args.health_timeout)
        except Exception as e:  # noqa: BLE001
            print(f"init 失败：{e}", file=sys.stderr)
            return EXIT_ENV
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            init_cmd.print_human(result)
        if not result["ok"]:
            return EXIT_ENV
        failed_health = [c for c in result.get("checks", []) if c["status"] != "ok"]
        return EXIT_HEALTH if failed_health else EXIT_OK

    if args.command == "down":
        try:
            result = down_cmd.run_down(cfg, lock, dry_run=args.dry_run,
                                       remove_volumes=not args.keep_volumes)
        except down_cmd.DownError as e:
            print(f"down 拒绝执行：{e}", file=sys.stderr)
            return EXIT_USAGE
        except Exception as e:  # noqa: BLE001
            print(f"down 失败：{e}", file=sys.stderr)
            return EXIT_ENV
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            down_cmd.print_human(result)
        return EXIT_OK if result["ok"] else EXIT_HEALTH

    if args.command == "bundle":
        try:
            if args.bundle_cmd == "create":
                result = bundle_cmd.create(cfg, lock, Path(args.out), mode=args.mode)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    bundle_cmd.print_create(result)
                return EXIT_OK
            result = bundle_cmd.verify(Path(args.dir))
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                bundle_cmd.print_verify(result)
            return EXIT_OK if result["ok"] else EXIT_DATA
        except bundle_cmd.BundleError as e:
            print(f"bundle 失败：{e}", file=sys.stderr)
            return EXIT_ENV

    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
