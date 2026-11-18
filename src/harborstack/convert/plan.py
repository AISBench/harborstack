"""span 构造层：TrialRecord → span 计划。纯 Python，不依赖 OTel。

映射关系
- Trial → Trace（root span ``trial``）
- agent Step（含 model_name/metrics）→ Generation span ``llm``
- ToolCall + Observation → Tool span ``tool.<function_name>``
- user/system Step → root 上的事件（保留任务指令与环境反馈上下文）
- subagent_trajectories（ATIF v1.7）→ root 下的汇总 span

时间语义
- step 级 timestamp 是 start 还是 end，由整条轨迹投票决定（记录票数与比例）
- 有 duration 时按 ``[ts-d, ts]`` 或 ``[ts, ts+d]`` 定位，否则退化为
  ``[ts, 下一步 ts]`` 的合并窗口，并如实标记 ``atif.timing_source``
- 工具 span 区间取 ``[本轮 LLM 结束, 下一步 LLM 起点]``，上界早于下界时夹为零长度
- 所有 span 的起止时间都非空且 ``end >= start``；缺失时退到 root 起点，
  root 也缺失时退到 EPOCH —— 绝不使用「导入时刻」，否则重复导入无法去重
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .sanitize import sanitize
from .scan import text_of

# 幂等 id 的命名空间。一旦发布不得修改：改了就等于换了一套 id，
# 同一 Trial 重复导入会在 Langfuse 里变成两条 trace。
_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "harborstack.atif")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class SpanSpec:
    name: str
    role: str                     # root | generation | tool | subagent
    attributes: dict[str, Any]
    span_id: int
    trace_id: int = 0             # 仅 root 携带，子 span 从父上下文继承
    start_ts: str | None = None
    end_ts: str | None = None
    parent: int | None = None     # 计划内父节点下标；0 即 root
    events: list[dict[str, Any]] = field(default_factory=list)
    clamped: bool = False         # 上界早于下界、被夹成零长度


class PlanError(Exception):
    """轨迹无法生成合法的 span 计划。"""


# ---------- 幂等 id ----------

def trace_key(trial) -> str:
    return f"harbor:{trial.run_id}/{trial.task_id}/{trial.trial_dir.name}"


def trace_id_of(trial) -> int:
    """128 bit；同 run + task + trial 目录名恒定。"""
    return int(uuid.uuid5(_NS, trace_key(trial)).hex, 16)


def span_id_of(trace_id: int, index: int, kind: str) -> int:
    """64 bit；取 uuid5 前 16 hex，恒不为 0（0 会被 SDK 当作非法 id 丢弃）。"""
    raw = int(uuid.uuid5(_NS, f"{trace_id:032x}:{index}:{kind}").hex[:16], 16)
    return raw or 1


# ---------- 时间 ----------

def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_ns(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


# 按字段存在性探测单步 LLM 耗时，不按 agent 身份分支。
DURATION_KEYS_MS = (
    "duration_ms",              # ATIF 标准字段
    "total_latency_ms",         # jiuwenswarm
    "llm_stream_duration_ms",   # kimi-cli（位于 step.extra）
    "latency_ms",
)

# 语义投票容差：排除时钟抖动与秒级精度带来的误判。
_SEMANTICS_EPS_S = 0.005
# 票型明确才启用 duration；落在中间带视为不确定，退回合并窗口。
_SEMANTICS_END_RATIO = 0.8
_SEMANTICS_START_RATIO = 0.2


def _probe_duration_ms(step) -> tuple[float | None, str | None]:
    holders = []
    metrics = step.metrics or {}
    if isinstance(metrics.get("extra"), dict):
        holders.append(("metrics.extra", metrics["extra"]))
    if isinstance(getattr(step, "extra", None), dict):
        holders.append(("step.extra", step.extra))
    for where, holder in holders:
        for key in DURATION_KEYS_MS:
            value = holder.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and value > 0:
                return float(value), f"{where}.{key}"
    return None, None


def detect_semantics(steps) -> tuple[str, dict]:
    """整条轨迹投票判定 timestamp 是 step 的起点还是终点。

    end 语义下 ``timestamp - duration`` 是本次调用的起点，必然不早于上一步；
    start 语义下它会退回到上一步区间内。
    """
    pos = neg = 0
    for i in range(1, len(steps)):
        cur, prev = steps[i], steps[i - 1]
        dur_ms, _ = _probe_duration_ms(cur)
        ts, prev_ts = parse_ts(cur.timestamp), parse_ts(prev.timestamp)
        if not dur_ms or ts is None or prev_ts is None:
            continue
        gap = (ts - timedelta(milliseconds=dur_ms) - prev_ts).total_seconds()
        if gap >= _SEMANTICS_EPS_S:
            pos += 1
        else:
            neg += 1

    votes = pos + neg
    if votes == 0:
        return "start", {"votes": 0}
    ratio = pos / votes
    stats = {"votes": votes, "end_ratio": round(ratio, 3)}
    if ratio >= _SEMANTICS_END_RATIO:
        return "end", stats
    if ratio <= _SEMANTICS_START_RATIO:
        return "start", stats
    # 票型分裂：不猜，调用方退回无 duration 的合并窗口
    return "ambiguous", stats


def _next_window_start(windows, idx: int) -> datetime | None:
    for j in range(idx + 1, len(windows)):
        window = windows[j]
        if window and window[0] is not None:
            return window[0]
    return None


def _error_attrs(trial) -> dict[str, Any]:
    """P0：harbor 的 ``exception_info`` 是全 agent 通用的失败信号。"""
    info = trial.exception
    if not isinstance(info, dict):
        return {}
    kind = str(info.get("exception_type") or "Exception")
    message = " ".join(str(info.get("exception_message") or "").split())
    text = f"{kind}: {message}" if message else kind
    return {
        "langfuse.observation.level": "ERROR",
        "langfuse.observation.status_message": text[:512],
        "error.type": kind,
        # 只拿到 trial 级信息，不伪装成某次 LLM 调用失败
        "atif.error_source": "harbor_result",
        "atif.error_scope": "trial",
    }


def _attach_trial_error(plan: list, trial) -> None:
    error = _error_attrs(trial)
    if not error:
        return
    last_gen = next((s for s in reversed(plan) if s.role == "generation"), None)
    if last_gen is not None:
        last_gen.attributes.update(error)


def _tool_output(step, tool_call_id: Any) -> str:
    """按 source_call_id 精确取该调用的观察结果，取不到才回落整段文本。"""
    if tool_call_id:
        for result in step.observation_results:
            if result.get("source_call_id") == tool_call_id:
                text = text_of(result.get("content"))
                if text:
                    return text
    return step.observation_text


def _generation_input(steps, index: int, task_prompt: str) -> str:
    """近似还原模型这一轮的输入：上一步 assistant 文本 + 工具观察。"""
    if index == 0:
        return task_prompt
    prev = steps[index - 1]
    parts = [text for text in (prev.message, prev.observation_text) if text]
    return "\n".join(parts) or task_prompt


def _derive_system(model_name: str | None) -> str:
    m = (model_name or "").lower()
    for prefix in ("openai", "anthropic", "google", "gemini", "meta-llama",
                   "mistral", "qwen", "deepseek", "zhipu", "baichuan"):
        if prefix in m:
            return {"gemini": "google", "meta-llama": "meta"}.get(prefix, prefix)
    return "unknown"


# ---------- 计划构造 ----------

def build_span_plan(trial, mode: str = "structural",
                    run_name: str | None = None,
                    dataset_id: str | None = None) -> list[SpanSpec]:
    steps = trial.steps
    semantics, timing_stats = detect_semantics(steps)
    trace_id = trace_id_of(trial)

    # 预计算每个 step 的时间窗口；None 表示该 step 不产生 LLM span。
    windows: list[tuple | None] = []
    for i, step in enumerate(steps):
        if not (step.source == "agent" and (step.model_name or step.metrics)):
            windows.append(None)
            continue
        ts = parse_ts(step.timestamp)
        next_ts = parse_ts(steps[i + 1].timestamp) if i + 1 < len(steps) else None
        dur_ms, dur_src = _probe_duration_ms(step)
        if ts is not None and dur_ms and semantics != "ambiguous":
            delta = timedelta(milliseconds=dur_ms)
            start, end = (ts - delta, ts) if semantics == "end" else (ts, ts + delta)
            source = f"measured:{dur_src}"
        elif ts is not None:
            # 无时长或语义不确定：只能给出合并窗口，LLM 与工具无法分离
            start, end = ts, next_ts or ts
            source = "inferred_window" if next_ts is not None else "unknown"
        else:
            start = end = None
            source = "unknown"
        windows.append((start, end, dur_ms, source))

    gen_starts = [w[0] for w in windows if w and w[0] is not None]
    gen_ends = [w[1] for w in windows if w and w[1] is not None]
    context_ts = [parse_ts(s.timestamp) for s in steps
                  if s.source in ("user", "system") and s.timestamp]
    root_starts = gen_starts + [t for t in context_ts if t is not None]
    root_start = min(root_starts) if root_starts else None
    root_end = max(gen_ends) if gen_ends else None

    root_span_id = span_id_of(trace_id, 0, "root")
    shared: dict[str, Any] = {
        "langfuse.session.id": trial.session_id,
        "langfuse.trace.name": f"{trial.task_id}#{trial.attempt}",
        "langfuse.trace.tags": ["harbor", trial.agent_name, trial.model_name],
        # Langfuse v4 用这组属性把多条 trace 归并成一次 experiment run，
        # 缺 item.id / root_observation_id 时 UI 不会聚合成实验
        "langfuse.experiment.id": trial.run_id,
        "langfuse.experiment.name": run_name or trial.run_id,
        "langfuse.experiment.item.id": trial.task_id,
        # 必须是 16 位十六进制字符串：span id 超过 int64 时整型属性会被丢弃
        "langfuse.experiment.item.root_observation_id": f"{root_span_id:016x}",
        "langfuse.user.id": trial.agent_name,
    }
    if dataset_id:
        shared["langfuse.experiment.dataset.id"] = dataset_id

    fm = trial.final_metrics or {}
    trace_metadata = {
        "langfuse.trace.metadata.run_id": trial.run_id,
        "langfuse.trace.metadata.job_name": run_name or trial.run_id,
        "langfuse.trace.metadata.total_prompt_tokens": fm.get("total_prompt_tokens"),
        "langfuse.trace.metadata.total_completion_tokens": fm.get("total_completion_tokens"),
        "langfuse.trace.metadata.total_cached_tokens": fm.get("total_cached_tokens"),
        "langfuse.trace.metadata.total_cost_usd": fm.get("total_cost_usd"),
        "langfuse.trace.metadata.total_steps": fm.get("total_steps"),
    }

    root_events = [{
        "name": f"step.{step.step_id}.{step.source}",
        "ts": step.timestamp,
        "attrs": {"message": sanitize(step.message, mode)},
    } for step in steps if step.source in ("user", "system") and step.message]

    root = SpanSpec(
        name="trial",
        role="root",
        span_id=root_span_id,
        trace_id=trace_id,
        attributes={
            **shared,
            **trace_metadata,
            **_error_attrs(trial),
            "langfuse.observation.input": sanitize(trial.task_prompt, mode),
            "langfuse.observation.output": sanitize(trial.final_message, mode),
            "atif.trial_dir": trial.trial_dir.name,
            "atif.timing_semantics": semantics,
            "atif.timing_missing": root_start is None,
            **{f"atif.timing_{k}": v for k, v in timing_stats.items()},
        },
        start_ts=root_start.isoformat() if root_start else None,
        end_ts=root_end.isoformat() if root_end else None,
        parent=None,
        events=root_events,
    )
    plan: list[SpanSpec] = [root]

    for i, step in enumerate(steps):
        window = windows[i]
        gen_idx: int | None = None
        if window is not None:
            start, end, dur_ms, source = window
            gen_attrs: dict[str, Any] = {
                **shared,
                "gen_ai.system": _derive_system(step.model_name),
                "gen_ai.request.model": step.model_name or "unknown",
                # ATIF 不保存原始 prompt：用上一步的 assistant 文本 + 工具观察近似，
                # 首次调用回落到任务指令，属有损还原
                "langfuse.observation.input": sanitize(
                    _generation_input(steps, i, trial.task_prompt), mode),
                "langfuse.observation.output": sanitize(step.message, mode),
                "atif.content_source": "atif_approximation",
                "atif.timing_source": source,
                "atif.timing_semantics": semantics,
            }
            if dur_ms:
                gen_attrs["atif.duration_ms"] = dur_ms
            metrics = step.metrics or {}
            if metrics.get("prompt_tokens") is not None:
                gen_attrs["gen_ai.usage.input_tokens"] = metrics["prompt_tokens"]
            if metrics.get("completion_tokens") is not None:
                gen_attrs["gen_ai.usage.output_tokens"] = metrics["completion_tokens"]
            if metrics.get("cached_tokens"):
                gen_attrs["gen_ai.usage.details.cached_tokens"] = metrics["cached_tokens"]
            if metrics.get("cost_usd") is not None:
                gen_attrs["atif.cost_usd"] = metrics["cost_usd"]
            if step.reasoning_content:
                gen_attrs["atif.reasoning"] = sanitize(step.reasoning_content, mode)

            gen_idx = len(plan)
            plan.append(SpanSpec(
                name="llm", role="generation", attributes=gen_attrs,
                span_id=span_id_of(trace_id, i, "llm"),
                start_ts=start.isoformat() if start else None,
                end_ts=end.isoformat() if end else None,
                parent=0,
            ))

        for k, call in enumerate(step.tool_calls or []):
            tool_attrs: dict[str, Any] = {
                **shared,
                "langfuse.observation.input": sanitize(call.get("arguments"), mode),
                "langfuse.observation.output": sanitize(
                    _tool_output(step, call.get("tool_call_id")), mode),
                "atif.tool_call_id": call.get("tool_call_id"),
                "atif.model_name": step.model_name,
            }
            if window is not None and window[3].startswith("measured"):
                # 模型调用结束后才有工具执行，上界是下一次调用的起点
                tool_start = window[1]
                tool_end = _next_window_start(windows, i) or window[1]
                tool_attrs["atif.timing_source"] = "measured_window"
            elif window is not None:
                tool_start, tool_end = window[0], window[1]
                tool_attrs["atif.timing_source"] = window[3]
            else:
                tool_start = tool_end = None
                tool_attrs["atif.timing_source"] = "unknown"

            plan.append(SpanSpec(
                name=f"tool.{call.get('function_name') or 'unknown'}",
                role="tool", attributes=tool_attrs,
                span_id=span_id_of(trace_id, i, f"tool.{k}"),
                start_ts=tool_start.isoformat() if tool_start else None,
                end_ts=tool_end.isoformat() if tool_end else None,
                parent=gen_idx if gen_idx is not None else 0,
            ))

    _attach_trial_error(plan, trial)

    for j, sub in enumerate(trial.subagent_trajectories or []):
        sub_agent = sub.get("agent") or {}
        sub_fm = sub.get("final_metrics") or {}
        sub_steps = sub.get("steps") or []
        plan.append(SpanSpec(
            name=f"subagent.{sub_agent.get('name') or j}",
            role="subagent",
            span_id=span_id_of(trace_id, j, "subagent"),
            attributes={
                **shared,
                "atif.subagent.model": sub_agent.get("model_name") or "unknown",
                "atif.subagent.steps": len(sub_steps),
                "atif.subagent.total_prompt_tokens": sub_fm.get("total_prompt_tokens"),
                "atif.subagent.total_completion_tokens": sub_fm.get("total_completion_tokens"),
            },
            start_ts=next((s.get("timestamp") for s in sub_steps
                           if s.get("timestamp")), None),
            end_ts=next((s.get("timestamp") for s in reversed(sub_steps)
                         if s.get("timestamp")), None),
            parent=0,
        ))

    _normalize(plan, root_start or EPOCH)
    return plan


def _normalize(plan: list[SpanSpec], fallback: datetime) -> None:
    """收敛全部时间：不留 None，不允许 end < start。被夹的 span 打上 clamped。"""
    fb = fallback.isoformat()
    for spec in plan:
        spec.start_ts = spec.start_ts or fb
        spec.end_ts = spec.end_ts or spec.start_ts
        if parse_ts(spec.end_ts) < parse_ts(spec.start_ts):
            spec.end_ts = spec.start_ts
            spec.clamped = True


def _duration_s(spec: SpanSpec) -> float:
    return (parse_ts(spec.end_ts) - parse_ts(spec.start_ts)).total_seconds()


def plan_stats(plan: list[SpanSpec]) -> dict[str, Any]:
    """对账用的汇总：类型分布、负时长、根区间、子区间覆盖与分角色时长。"""
    counts: dict[str, int] = {}
    durations: dict[str, float] = {}
    for spec in plan:
        counts[spec.role] = counts.get(spec.role, 0) + 1
        durations[spec.role] = durations.get(spec.role, 0.0) + _duration_s(spec)
    root = plan[0]
    root_start, root_end = parse_ts(root.start_ts), parse_ts(root.end_ts)
    negative = sum(
        1 for spec in plan
        if parse_ts(spec.end_ts) < parse_ts(spec.start_ts)
    )
    outside = 0
    for spec in plan[1:]:
        start, end = parse_ts(spec.start_ts), parse_ts(spec.end_ts)
        if start < root_start or end > root_end:
            outside += 1
    return {
        "counts": counts,
        "durations": {k: round(v, 3) for k, v in durations.items()},
        "spans": len(plan),
        "negative": negative,
        "clamped": sum(1 for spec in plan if spec.clamped),
        "outside_root": outside,
        "root_s": (root_end - root_start).total_seconds(),
    }
