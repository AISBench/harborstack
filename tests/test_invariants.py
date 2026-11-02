"""金样端到端不变量。

``fixtures/`` 下有两条真实 ATIF，均由 harbor 真机产物拷贝而来：

- ``atif-fidelity``：成功 trial（reward=1，101 span）
- ``atif-error``：402 失败 trial（reward=0，root + 最后一个 llm 标 ERROR）

期望值固化在 ``fixtures/expected.json``。改动转换逻辑若改变了这些数值，
必须同步更新该文件并在提交说明里写明原因。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harborstack.convert import plan as planmod
from harborstack.convert import scan as scanmod

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EXPECTED = json.loads(
    (FIXTURES / "expected.json").read_text(encoding="utf-8"))["fixtures"]
NAMES = ("atif-fidelity", "atif-error")

SNAPSHOT_KEYS = ("spans", "counts", "durations",
                 "negative", "clamped", "outside_root")


@pytest.fixture(scope="module", params=NAMES)
def built(request):
    name = request.param
    records, skipped, missing = scanmod.load_trials(FIXTURES / name)
    assert skipped == 0, f"{name} 出现解析失败的 Trial"
    assert missing == [], f"{name} 缺 ATIF"
    assert len(records) == 1, f"{name} 应恰好一条 Trial"
    trial = records[0]
    plan = planmod.build_span_plan(trial, mode="full", run_name="fixture")
    return name, trial, plan


def test_snapshot_matches_frozen_values(built):
    """类型分布、时长、根区间与 trace_id 必须与冻结值完全一致。"""
    name, trial, plan = built
    expected = EXPECTED[name]
    stats = planmod.plan_stats(plan)

    assert planmod.trace_id_of(trial) == expected["trace_id"]
    for key in SNAPSHOT_KEYS:
        assert stats[key] == expected[key], f"{name}.{key} 偏离冻结值"
    assert trial.reward == expected["reward"]


def test_time_invariants(built):
    """起止时间非空、end >= start、子 span 不越出 root。"""
    name, _trial, plan = built
    root_start = planmod.parse_ts(plan[0].start_ts)
    root_end = planmod.parse_ts(plan[0].end_ts)

    for spec in plan:
        start, end = planmod.parse_ts(spec.start_ts), planmod.parse_ts(spec.end_ts)
        assert start is not None and end is not None, f"{name}:{spec.name} 时间为空"
        assert end >= start, f"{name}:{spec.name} 出现负时长"
        if spec.role == "root":
            continue
        assert start >= root_start and end <= root_end, \
            f"{name}:{spec.name} 越出 root 区间"


def test_no_span_longer_than_root(built):
    """单 span 时长不得超过 root 的 1.5 倍（70 小时问题的探针）。"""
    name, _trial, plan = built
    stats = planmod.plan_stats(plan)
    root_s = stats["root_s"]
    assert round(stats["root_s"], 3) == EXPECTED[name]["root_s"]
    for spec in plan[1:]:
        duration = planmod._duration_s(spec)
        assert duration <= root_s * 1.5, f"{name}:{spec.name} 时长 {duration}s 异常"


def test_plan_is_deterministic(built):
    """同一 Trial 连续构建两次，id 与时间戳逐 span 一致。"""
    name, trial, plan = built
    again = planmod.build_span_plan(trial, mode="full", run_name="fixture")
    first = [(s.span_id, s.start_ts, s.end_ts) for s in plan]
    second = [(s.span_id, s.start_ts, s.end_ts) for s in again]
    assert first == second, f"{name} 构建结果不稳定"


def test_tool_output_follows_source_call_id(built):
    """并行工具调用必须各取各的观察结果，不能共享整段拼接文本。"""
    name, trial, plan = built
    tools = [s for s in plan if s.role == "tool"]
    flattened = []
    for step in trial.steps:
        for call in step.tool_calls or []:
            results = {r.get("source_call_id"): scanmod.text_of(r.get("content"))
                       for r in step.observation_results
                       if r.get("source_call_id")}
            flattened.append((results.get(call.get("tool_call_id")),
                              len(step.tool_calls or [])))

    assert len(tools) == len(flattened), f"{name} tool span 数量与调用数不一致"

    checked = 0
    for spec, (expected_output, _count) in zip(tools, flattened):
        if expected_output is None:
            continue
        assert spec.attributes["langfuse.observation.output"] == expected_output, \
            f"{name}:{spec.name} 未取到自己的观察结果"
        checked += 1

    if name == "atif-fidelity":
        assert checked >= 50, "金样中应覆盖全部可匹配的工具调用"
        multi = [n for _o, n in flattened if n >= 2]
        assert multi, "金样应至少有一个并行工具调用步骤"


def test_error_level_marking(built):
    """成功样本无 ERROR；失败样本只在 root 与最后一个 generation 标记 ERROR。"""
    name, trial, plan = built
    marked = [s for s in plan
              if s.attributes.get("langfuse.observation.level") == "ERROR"]

    if not trial.exception:
        assert not marked, f"{name} 不应有 ERROR 标记"
        return

    gens = [s for s in plan if s.role == "generation"]
    assert {id(s) for s in marked} == {id(plan[0]), id(gens[-1])}
    for spec in marked:
        assert spec.attributes["atif.error_scope"] == "trial"
        assert spec.attributes["atif.error_source"] == "harbor_result"
        assert spec.attributes["error.type"] == \
            trial.exception.get("exception_type")
        assert spec.attributes["langfuse.observation.status_message"].startswith(
            EXPECTED[name]["error"]["status_message_prefix"])
