"""display：健康校验 → 扫描 Trial → 上报 Langfuse → 判分回填 → report.json。

幂等：trace_id / span_id / start_time 全部由 Trial 内容推导（见 ``convert.plan``）。
Langfuse v4 events 模式不会按 span_id 去重，重复上报会产生重复 observation，
因此导入前先用 ``v2/observations`` 查询 trace 是否已有数据，存在则跳过。
``--dry-run`` 只做体检与计划，不产生任何网络写入。
"""

from __future__ import annotations

import base64
import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from harborstack import config as cfgmod
from harborstack import manifest
from harborstack.convert import plan as planmod
from harborstack.convert import scan
from harborstack.convert import scores as scoresmod
from harborstack.deploy import render

REPORT_NAME = "report.json"
SCHEMA = 1


class DisplayError(Exception):
    """run 目录、凭据或服务状态不满足导入前提。"""


# ---------- 定位 ----------

def resolve_run_dir(cfg: dict, explicit: str | None = None) -> Path:
    if explicit:
        path = cfgmod.expand_path(explicit)
        if not path.is_dir():
            raise DisplayError(f"run 目录不存在：{path}")
        return path

    runs = cfgmod.expand_path(cfg["paths"]["var_dir"]) / "runs"
    if not runs.is_dir():
        raise DisplayError(f"没有 run 目录：{runs}；先执行 harborstack run")
    dirs = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.name)
    if not dirs:
        raise DisplayError(f"{runs} 下没有 run 记录；先执行 harborstack run")
    return dirs[-1]


def load_run_meta(run_dir: Path) -> dict:
    path = run_dir / "run.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# ---------- 健康校验 ----------

def _http_status(url: str, timeout: float) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, OSError) as exc:
        return None, str(getattr(exc, "reason", exc))[:160]


def trace_exists(host: str, public_key: str, secret_key: str,
                 trace_id: str, timeout: float = 10.0) -> bool:
    """查询 trace 是否已有观测。

    v4 ``events_only`` 模式下 ``/api/public/traces`` 不可用，但
    ``/api/public/v2/observations`` 仍可读，用它做存在性判断。
    """
    url = f"{host.rstrip('/')}/api/public/v2/observations?traceId={trace_id}&limit=1"
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read() or b"{}")
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return False
    return bool(payload.get("data"))


def _settle(host: str, public_key: str, secret_key: str, entries: list[dict],
            seconds: float, interval: float = 1.0) -> dict | None:
    """等最后一个**成功导入**的 trace 在 API 里可查。

    Langfuse 的写入是最终一致的：OTLP/score 请求返回 2xx 只代表已接收。
    这里只做有界等待，超时不改变退出码，只提示用户稍后刷新。
    """
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    anchor = next((e for e in reversed(entries)
                   if (e.get("trace") or {}).get("id")
                   and e.get("status") in ("ok", "agent_error")), None)
    if anchor is None:
        return None
    trace_id = anchor["trace"]["id"]
    start = time.monotonic()
    deadline = start + seconds
    while True:
        if trace_exists(host, public_key, secret_key, trace_id, timeout=5.0):
            return {"trace_id": trace_id, "visible": True,
                    "waited_s": round(time.monotonic() - start, 1)}
        if time.monotonic() >= deadline:
            return {"trace_id": trace_id, "visible": False,
                    "waited_s": round(time.monotonic() - start, 1)}
        time.sleep(interval)


def _stack(cfg: dict) -> tuple[dict, dict[str, int]]:
    man = manifest.load(manifest.path_for(cfg)) or {}
    ports = render.read_ports(cfgmod.expand_path(cfg["paths"]["var_dir"]))
    if not ports:
        ports = {k: int(v) for k, v in (man.get("ports") or {}).items()}
    return man, ports


def check_health(cfg: dict, timeout: float = 10) -> list[dict]:
    """display 只做两段便宜检查：manifest ready + web 健康端点 200。"""
    checks: list[dict] = []
    man, ports = _stack(cfg)

    if not man:
        checks.append({"id": "manifest", "status": "fail",
                       "detail": "未找到 var/env-manifest.json，先执行 harborstack init"})
    else:
        ok = man.get("state") == manifest.READY
        checks.append({"id": "manifest", "status": "ok" if ok else "fail",
                       "detail": f"state={man.get('state')}"})

    web = ports.get("web")
    if not web:
        checks.append({"id": "web", "status": "fail", "detail": "未分配 web 端口"})
    else:
        url = f"http://127.0.0.1:{web}/api/public/health"
        status, detail = _http_status(url, timeout)
        checks.append({"id": "web", "status": "ok" if status == 200 else "fail",
                       "detail": f"{url} -> {status or detail}"})
    return checks


def host_url(cfg: dict) -> str:
    _, ports = _stack(cfg)
    if "web" not in ports:
        raise DisplayError("未找到 web 端口，先执行 harborstack init")
    return f"http://127.0.0.1:{ports['web']}"


def load_credentials(cfg: dict) -> tuple[str, str]:
    path = cfgmod.expand_path(cfg["paths"]["var_dir"]) / render.SECRETS_NAME
    if not path.is_file():
        raise DisplayError(f"缺少 {path}；先执行 harborstack init 生成 API Key")
    try:
        values = json.loads(path.read_text(encoding="utf-8")).get("values") or {}
    except json.JSONDecodeError as exc:
        raise DisplayError(f"{path} 不是合法 JSON：{exc}") from exc
    public_key, secret_key = values.get("public_key"), values.get("secret_key")
    if not public_key or not secret_key:
        raise DisplayError(f"{path} 缺少 public_key / secret_key")
    return public_key, secret_key


# ---------- 对账 ----------

def _sub_span_outside_root(plan: list[planmod.SpanSpec]) -> int:
    """subagent 的时间来自独立轨迹，不参与 root 覆盖判定。"""
    root = plan[0]
    root_start, root_end = planmod.parse_ts(root.start_ts), planmod.parse_ts(root.end_ts)
    count = 0
    for spec in plan[1:]:
        if spec.role == "subagent":
            continue
        start, end = planmod.parse_ts(spec.start_ts), planmod.parse_ts(spec.end_ts)
        if start < root_start or end > root_end:
            count += 1
    return count


def _timing(plan: list[planmod.SpanSpec], stats: dict) -> tuple[dict, bool | None]:
    root = plan[0]
    attrs = root.attributes
    gen = [spec for spec in plan if spec.role == "generation"]
    measured = [spec.attributes.get("atif.duration_ms") for spec in gen]
    known = [float(v) for v in measured if isinstance(v, (int, float))]
    actual_ms = stats["durations"].get("generation", 0.0) * 1000

    # 语义投票不确定时计划退回合并窗口，时长不再是实测值，等式不适用
    all_measured = bool(gen) and all(
        str(spec.attributes.get("atif.timing_source", "")).startswith("measured")
        for spec in gen)
    if not all_measured:
        llm_sum_match: bool | None = None
    else:
        llm_sum_match = abs(actual_ms - sum(known)) <= 1.0

    timing = {
        "semantics": attrs.get("atif.timing_semantics"),
        "votes": attrs.get("atif.timing_votes", 0),
        "end_ratio": attrs.get("atif.timing_end_ratio"),
        "root_s": round(stats["root_s"], 3),
        "llm_s": stats["durations"].get("generation", 0.0),
        "tool_s": stats["durations"].get("tool", 0.0),
        "clamped": stats["clamped"],
    }
    return timing, llm_sum_match


def _reward_match(results: list[tuple[str, int | None, str]]) -> bool | None:
    """v4 的 Public API 可能直接 404，此时记为不适用而不是失败。"""
    item = next((r for r in results if r[0] == scoresmod.REWARD_SCORE), None)
    if item is None:
        return None
    name, status, _ = item
    if status is None or status in (404, 405):
        return None
    return status < 300


def _trace_url(host: str, trace_id: str) -> str:
    return f"{host}/project/{render.PROJECT_ID}/traces/{trace_id}"


# ---------- 主流程 ----------

def build_entry(rec, plan: list[planmod.SpanSpec], stats: dict,
                host: str) -> dict:
    timing, llm_sum_match = _timing(plan, stats)
    trace_id = format(plan[0].trace_id, "032x")
    return {
        "name": rec.task_id,
        "trial_id": rec.trial_dir.name,
        "trace": {"id": trace_id, "url": _trace_url(host, trace_id)},
        "counts": {
            "events": len(plan),
            "generation": stats["counts"].get("generation", 0),
            "span": len(plan) - stats["counts"].get("generation", 0),
        },
        "timing": timing,
        "reconcile": {
            "reward": rec.reward,
            "reward_match": None,
            "llm_sum_match": llm_sum_match,
            "orphan_spans": 0,
            "negative": stats["negative"],
            "outside_root": _sub_span_outside_root(plan),
        },
        "scores": [],
        "exception": rec.exception,
        "status": "ok",
        "error": None,
    }


def _argv_value(argv: list[str], *names: str) -> str:
    """从 harbor argv 里取参数值（支持 ``--x value`` 与 ``--x=value``）。"""
    for i, arg in enumerate(argv):
        for name in names:
            if arg == name and i + 1 < len(argv):
                return argv[i + 1]
            if arg.startswith(f"{name}="):
                return arg.split("=", 1)[1]
    return ""


def run_name_of(meta: dict, root: Path) -> str:
    """Langfuse 里一次评测的显示名：优先 run.json 的 job_name，其次 argv 里的 --job-name，最后 run 目录名。"""
    argv = [str(a) for a in (meta.get("argv") or [])]
    return meta.get("job_name") or _argv_value(argv, "--job-name") or root.name


def dataset_id_of(meta: dict, root: Path) -> str:
    """实验所属的数据集标识：harbor 的 ``-p`` 目录名，缺省回落到 job 目录名。"""
    argv = [str(a) for a in (meta.get("argv") or [])]
    path = _argv_value(argv, "-p", "--path", "--dataset")
    if path:
        return Path(path).name or path
    return root.name


def run_display(cfg: dict, run_dir: str | None = None, dry_run: bool = False,
                trial_filter: str = "all", wait_seconds: float | None = None,
                log=print) -> dict:
    path = resolve_run_dir(cfg, run_dir)
    meta = load_run_meta(path)
    root = Path(meta["jobs_dir"]) if meta.get("jobs_dir") else path
    if not root.is_dir():
        raise DisplayError(f"run.json 记录的 jobs 目录不存在：{root}")

    checks = check_health(cfg)
    if any(c["status"] != "ok" for c in checks):
        return {"ok": False, "checks": checks, "report": None, "exit": 4}

    host = host_url(cfg)
    skipped_entries: list[dict] = []

    def _on_skip(path: Path, exc: Exception) -> None:
        log(f"[skip] {path.name}: {exc}")
        skipped_entries.append({
            "trial": path.name, "path": str(path.resolve()),
            "kind": "parse_error", "reason": str(exc),
        })

    records, skipped, missing = scan.load_trials(
        root, trial_filter=trial_filter,
        on_skip=_on_skip,
        on_missing=lambda p, r: log(f"[miss] {p.name}: {r}"))

    exporter = None
    if not dry_run:
        public_key, secret_key = load_credentials(cfg)
        from harborstack.convert.export import Exporter

        exporter = Exporter(host, public_key, secret_key)

    run_name = run_name_of(meta, root)
    dataset_id = dataset_id_of(meta, root)
    entries: list[dict] = []
    for rec in records:
        try:
            plan = planmod.build_span_plan(
                rec, mode=cfg["export"]["mode"],
                run_name=run_name, dataset_id=dataset_id)
            stats = planmod.plan_stats(plan)
            entry = build_entry(rec, plan, stats, host)
        except Exception as exc:  # noqa: BLE001 - 单个 Trial 坏掉不拖垮整批
            entries.append({"name": rec.task_id, "trial_id": rec.trial_dir.name,
                            "status": "failed", "error": f"计划构造失败：{exc}",
                            "trace": None, "counts": {}, "timing": {},
                            "reconcile": {"reward": rec.reward}, "scores": []})
            continue

        if exporter is not None:
            if trace_exists(host, public_key, secret_key, entry["trace"]["id"]):
                entry["status"] = "skipped"
                entry["error"] = "trace 已存在，跳过重复导入"
                entries.append(entry)
                continue
            try:
                ids = exporter.send(plan)
                exporter.flush()
                entry["scores"] = [
                    {"name": name, "status": status, "detail": detail}
                    for name, status, detail in scoresmod.backfill(
                        host, public_key, secret_key, ids, rec,
                        timestamp=plan[0].start_ts)
                ]
                entry["reconcile"]["reward_match"] = _reward_match(
                    [(s["name"], s["status"], s["detail"]) for s in entry["scores"]])
            except Exception as exc:  # noqa: BLE001
                entry["status"] = "failed"
                entry["error"] = f"上报失败：{exc}"
                entries.append(entry)
                continue

        if rec.exception:
            entry["status"] = "agent_error"
            entry["error"] = _exception_text(rec.exception)
        else:
            entry["status"] = "ok" if entry["reconcile"]["negative"] == 0 \
                and entry["reconcile"]["outside_root"] == 0 else "failed"
        entries.append(entry)

    if exporter is not None:
        exporter.shutdown()

    settle = None
    if exporter is not None:
        wait = cfg["display"]["wait_seconds"] if wait_seconds is None else wait_seconds
        settle = _settle(host, public_key, secret_key, entries, wait)

    man, _ = _stack(cfg)
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run": dry_run,
        "run": {"dir": str(path), "jobs_dir": str(root),
                "trials": len(records), "skipped": skipped,
                "missing": len(missing)},
        "harbor": meta.get("harbor") or {},
        "env": {
            "web": host,
            "clickhouse_digest": ((man.get("images") or {}).get("clickhouse") or {}).get("digest"),
            "clickhouse_bin_sha256": ((man.get("binaries") or {}).get("clickhouse") or {}).get("sha256"),
        },
        "trials": entries,
        "settle": settle,
        "skipped": skipped_entries,
        "missing": missing,
        "status": "ok",
    }
    failed = (not entries) or skipped > 0 or any(
        e["status"] not in ("ok", "skipped") for e in entries)
    report["status"] = "failed" if failed else "ok"

    if not dry_run:
        try:
            _write_report(path, cfg, report)
        except OSError as exc:
            raise DisplayError(f"report.json 写入失败：{exc}") from exc

    return {"ok": not failed, "checks": checks, "report": report,
            "exit": 5 if failed else 0}


def _exception_text(info: dict) -> str:
    kind = info.get("exception_type") or "Exception"
    message = info.get("exception_message") or ""
    return f"{kind}: {message}" if message else str(kind)


def _write_report(run_path: Path, cfg: dict, report: dict) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    for target in (run_path / REPORT_NAME,
                   cfgmod.expand_path(cfg["report"]["path"])):
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)


def print_human(result: dict) -> None:
    checks = result.get("checks") or []
    for check in checks:
        mark = "ok  " if check["status"] == "ok" else "FAIL"
        print(f"[{mark}] {check['id']:<10} {check['detail']}")

    report = result.get("report")
    if not report:
        return
    if checks:
        print()

    for entry in report["trials"]:
        if entry["status"] == "failed" and entry.get("error"):
            print(f"[FAIL] {entry['trial_id']}  {entry['error']}")
            continue
        if entry["status"] == "skipped":
            print(f"[skip] {entry['trial_id']}  {entry.get('error') or ''}")
            continue
        counts, timing = entry["counts"], entry["timing"]
        print(f"[{ 'plan' if report['dry_run'] else 'ok  ' }] "
              f"{entry['trial_id']}  "
              f"events={counts['events']} gen={counts['generation']} "
              f"span={counts['span']}  root={timing.get('root_s')}s")
        if entry.get("exception"):
            print(f"[WARN] {entry['trial_id']}  agent 异常：{entry['error']}")

    print(f"\nrun        {report['run']['dir']}")
    print(f"trials     {report['run']['trials']}"
          f"（跳过 {report['run']['skipped']}，缺文件 {report['run'].get('missing', 0)}）")
    print(f"Langfuse   {report['env']['web']}")
    for entry in report["trials"]:
        if entry.get("trace"):
            print(f"trace      {entry['trace']['url']}")
    if report["dry_run"]:
        print("模式       dry-run（未上报、未写 report.json）")
    else:
        print(f"report     {Path(report['run']['dir']) / REPORT_NAME}")
        print(f"状态       {report['status']}")
    settle = report.get("settle")
    if settle:
        if settle["visible"]:
            print(f"[ok  ] settle     等待 {settle['waited_s']}s 后 trace 已可查询")
        else:
            print(f"[warn] settle     等待 {settle['waited_s']}s 后仍不可查；"
                  "数据仍在入库，请稍后刷新页面，不要重复执行 display")
