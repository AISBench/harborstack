"""转换器的幂等 id、时间不变量、读取容错，以及 case 目录发现。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import harborstack.convert.sanitize as sanmod
from harborstack.convert import plan as planmod
from harborstack.convert import scan

BASE = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


# ---------------- fixture 构造 ----------------

def _iso(offset_s: float) -> str:
    return (BASE + timedelta(seconds=offset_s)).isoformat()


def _steps(specs: list[dict]) -> list[dict]:
    steps = []
    for i, spec in enumerate(specs, start=1):
        step = {"step_id": i, "source": spec["source"],
                "message": spec.get("message", f"message-{i}")}
        if spec.get("at") is not None:
            step["timestamp"] = _iso(spec["at"])
        if spec["source"] == "agent":
            step["model_name"] = spec.get("model", "openai/deepseek-chat")
            step["metrics"] = {
                "prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 0,
                "extra": {"total_latency_ms": spec.get("dur_ms", 0)},
            }
            if spec.get("tools"):
                step["tool_calls"] = [
                    {"tool_call_id": f"c{i}-{k}", "function_name": name,
                     "arguments": {"command": name}}
                    for k, name in enumerate(spec["tools"])
                ]
                step["observation"] = {"results": [{"content": "ok", "source_call_id": f"c{i}"}]}
        steps.append(step)
    return steps


def _make_trial(root: Path, specs: list[dict] | None = None,
                name: str = "task-a__openjiuwen__openai__1",
                reward: str | None = "1.0") -> Path:
    specs = specs or [
        {"source": "user", "at": 0.0, "message": "solve it"},
        {"source": "agent", "at": 6.0, "dur_ms": 2000, "tools": ["bash"]},
        {"source": "agent", "at": 12.0, "dur_ms": 3000, "tools": ["bash", "read"]},
    ]
    trial = root / name
    (trial / "agent").mkdir(parents=True)
    (trial / "agent" / "trajectory.json").write_text(json.dumps({
        "schema_version": "1.7",
        "session_id": f"session-{name}",
        "agent": {"name": "openjiuwen", "model_name": "openai/deepseek-chat"},
        "steps": _steps(specs),
        "final_metrics": {"total_prompt_tokens": 30, "total_completion_tokens": 10,
                          "total_steps": len(specs)},
    }), encoding="utf-8")
    if reward is not None:
        (trial / "verifier").mkdir()
        (trial / "verifier" / "reward.txt").write_text(reward, encoding="utf-8")
    return trial


@pytest.fixture()
def trial(tmp_path: Path):
    return scan.load_trial(_make_trial(tmp_path), run_id_hint="run-1")


# ---------------- 幂等 id ----------------

def test_trace_id_is_stable_and_32_hex(trial, tmp_path: Path):
    first = planmod.trace_id_of(trial)
    again = planmod.trace_id_of(scan.load_trial(trial.trial_dir, run_id_hint="run-1"))

    assert first == again
    assert len(f"{first:032x}") == 32
    assert first != 0

    other = scan.load_trial(_make_trial(tmp_path / "b",
                                        name="task-a__openjiuwen__openai__2"),
                            run_id_hint="run-1")
    assert planmod.trace_id_of(other) != first


def test_span_ids_are_deterministic_and_unique(trial):
    first = planmod.build_span_plan(trial)
    second = planmod.build_span_plan(trial)

    ids = [spec.span_id for spec in first]
    assert ids == [spec.span_id for spec in second]
    assert len(set(ids)) == len(ids), "span_id 重复会让 Langfuse 覆盖掉 span"
    assert all(len(f"{sid:016x}") == 16 and sid != 0 for sid in ids)
    assert first[0].trace_id == planmod.trace_id_of(trial)


def test_span_id_changes_when_trial_identity_changes(trial, tmp_path: Path):
    other = scan.load_trial(_make_trial(tmp_path / "b",
                                        name="task-a__openjiuwen__openai__2"),
                            run_id_hint="run-1")
    mine = {spec.span_id for spec in planmod.build_span_plan(trial)}
    theirs = {spec.span_id for spec in planmod.build_span_plan(other)}
    assert not (mine & theirs)


# ---------------- 时间不变量 ----------------

def test_no_null_and_no_negative_windows(trial):
    plan = planmod.build_span_plan(trial)
    for spec in plan:
        assert spec.start_ts, f"{spec.name} 缺少 start_time"
        assert spec.end_ts, f"{spec.name} 缺少 end_time"
        assert planmod.parse_ts(spec.end_ts) >= planmod.parse_ts(spec.start_ts)


def test_children_stay_inside_root(trial):
    stats = planmod.plan_stats(planmod.build_span_plan(trial))
    assert stats["outside_root"] == 0
    assert stats["negative"] == 0
    assert stats["root_s"] == pytest.approx(12.0)


def test_root_starts_at_first_atif_timestamp(trial):
    plan = planmod.build_span_plan(trial)
    assert planmod.parse_ts(plan[0].start_ts) == BASE


def test_plan_does_not_depend_on_wall_clock(trial):
    """重复导入必须得到同一份时间，否则 events_full 去重键永远对不上。"""
    first = [(spec.start_ts, spec.end_ts) for spec in planmod.build_span_plan(trial)]
    second = [(spec.start_ts, spec.end_ts) for spec in planmod.build_span_plan(trial)]
    assert first == second


def test_missing_timestamps_fall_back_to_epoch(tmp_path: Path):
    trial = scan.load_trial(_make_trial(tmp_path, specs=[
        {"source": "user", "at": None, "message": "no ts"},
        {"source": "agent", "at": None, "dur_ms": 0, "tools": ["bash"]},
    ]))
    plan = planmod.build_span_plan(trial)

    assert planmod.parse_ts(plan[0].start_ts) == planmod.EPOCH
    assert plan[0].attributes["atif.timing_missing"] is True
    assert all(spec.start_ts for spec in plan)


def test_measured_durations_match_metrics_sum(trial):
    plan = planmod.build_span_plan(trial)
    total = sum(spec.attributes.get("atif.duration_ms", 0)
                for spec in plan if spec.role == "generation")
    expected = sum(step.metrics["extra"]["total_latency_ms"]
                   for step in trial.steps if step.source == "agent")
    assert total == pytest.approx(expected, abs=1)


def test_semantics_vote_picks_end_when_timestamps_mark_end(trial):
    plan = planmod.build_span_plan(trial)
    assert plan[0].attributes["atif.timing_semantics"] == "end"
    assert plan[0].attributes["atif.timing_votes"] == 2
    llm = [spec for spec in plan if spec.role == "generation"]
    assert all(spec.attributes["atif.timing_source"].startswith("measured:") for spec in llm)


def test_semantics_without_duration_uses_inferred_window(tmp_path: Path):
    trial = scan.load_trial(_make_trial(tmp_path, specs=[
        {"source": "user", "at": 0.0},
        {"source": "agent", "at": 4.0, "dur_ms": 0, "tools": ["bash"]},
        {"source": "agent", "at": 9.0, "dur_ms": 0},
    ]))
    plan = planmod.build_span_plan(trial)
    assert plan[0].attributes["atif.timing_semantics"] == "start"
    llm = [spec for spec in plan if spec.role == "generation"]
    # 末步没有下一步可作上界，只能退化到零长度
    assert [spec.attributes["atif.timing_source"] for spec in llm] == [
        "inferred_window", "unknown"]
    assert planmod.parse_ts(llm[0].end_ts) == planmod.parse_ts(llm[1].start_ts)
    assert planmod.parse_ts(llm[1].end_ts) == planmod.parse_ts(llm[1].start_ts)


def test_tool_window_is_clamped_when_next_start_precedes(tmp_path: Path):
    """下一步的起点早于本轮结束（毫秒级抖动）时，夹成零长度而非负时长。"""
    trial = scan.load_trial(_make_trial(tmp_path, specs=[
        {"source": "user", "at": 0.0},
        {"source": "agent", "at": 6.0, "dur_ms": 2000},
        {"source": "agent", "at": 12.0, "dur_ms": 2000},
        {"source": "agent", "at": 18.0, "dur_ms": 2000},
        {"source": "agent", "at": 24.0, "dur_ms": 2000},
        {"source": "agent", "at": 30.0, "dur_ms": 2000, "tools": ["bash"]},
        {"source": "agent", "at": 31.0, "dur_ms": 5000},
    ]))
    plan = planmod.build_span_plan(trial)
    assert plan[0].attributes["atif.timing_semantics"] == "end"

    tool = next(spec for spec in plan if spec.role == "tool")
    assert planmod.parse_ts(tool.end_ts) >= planmod.parse_ts(tool.start_ts)
    assert tool.end_ts == tool.start_ts


def test_tool_span_follows_generation_end(trial):
    plan = planmod.build_span_plan(trial)
    first_tool = next(spec for spec in plan if spec.role == "tool")
    parent = plan[first_tool.parent]
    assert parent.role == "generation"
    assert planmod.parse_ts(first_tool.start_ts) == planmod.parse_ts(parent.end_ts)


# ---------------- 脱敏 ----------------

def test_structural_mode_keeps_structure_drops_content():
    value = {"cmd": "rm -rf /", "n": 3, "flag": True, "items": ["a"]}
    out = sanmod.sanitize(value, "structural")

    assert out["cmd"] == {"_len": 8, "_sha256_8": sanmod._sha8("rm -rf /")}
    assert out["n"] == 3 and out["flag"] is True
    assert out["items"] == [{"_len": 1, "_sha256_8": sanmod._sha8("a")}]


def test_full_mode_truncates_long_text():
    out = sanmod.sanitize("x" * (sanmod.MAX_FULL_LEN + 10), "full")
    assert out.endswith("...[truncated 65546 chars]")


# ---------------- 读取层的容错 ----------------

def test_reward_txt_wins_over_json(tmp_path: Path):
    trial_dir = _make_trial(tmp_path, reward="0.75")
    (trial_dir / "verifier" / "reward.json").write_text(
        json.dumps({"reward": 0.25}), encoding="utf-8")
    reward, details = scan.load_reward(trial_dir)
    assert reward == 0.75
    assert details == {}


def test_reward_json_details_are_parsed(tmp_path: Path):
    trial_dir = _make_trial(tmp_path, reward=None)
    (trial_dir / "verifier").mkdir()
    (trial_dir / "verifier" / "reward.json").write_text(json.dumps({
        "reward": 0.5, "details": {"a": 1, "b": 0.0, "c": "skip"}}), encoding="utf-8")
    reward, details = scan.load_reward(trial_dir)
    assert reward == 0.5
    assert details == {"a": 1.0, "b": 0.0}


def test_reward_details_rubric_is_flattened(tmp_path: Path):
    trial_dir = _make_trial(tmp_path, reward=None)
    (trial_dir / "verifier").mkdir()
    (trial_dir / "verifier" / "reward-details.json").write_text(json.dumps({
        "criteria": [{"name": "c1", "score": 1}, {"name": "c2", "score": 0}],
    }), encoding="utf-8")
    assert scan.load_reward_details(trial_dir) == {"c1": 1.0, "c2": 0.0}


def test_load_trials_discovers_nested_and_skips_broken(tmp_path: Path):
    _make_trial(tmp_path / "run" / "batch-1", name="t1__agent__model__1")
    _make_trial(tmp_path / "run" / "batch-2", name="t2__agent__model__1")
    broken = tmp_path / "run" / "batch-2" / "t3__agent__model__1" / "agent"
    broken.mkdir(parents=True)
    (broken / "trajectory.json").write_text("{not json", encoding="utf-8")

    records, skipped, missing = scan.load_trials(tmp_path, run_id_hint="run-1")

    assert len(records) == 2
    assert skipped == 1
    assert missing == []
    assert {r.trial_dir.name for r in records} == {"t1__agent__model__1", "t2__agent__model__1"}


def test_load_trials_reports_case_without_atif(tmp_path: Path):
    job = tmp_path / "job-1"
    _make_trial(job, name="ok__agent__model__1")
    (job / "result.json").write_text("{}", encoding="utf-8")  # job 根目录不算 case
    orphan = job / "broken__agent__model__1"
    orphan.mkdir(parents=True)
    (orphan / "result.json").write_text(json.dumps({
        "exception_info": {"exception_type": "NonZeroAgentExitCodeError",
                           "exception_message": "jiuwenswarm run failed (402)"},
    }), encoding="utf-8")

    records, skipped, missing = scan.load_trials(job)

    assert [r.task_id for r in records] == ["ok"]
    assert skipped == 0, "缺 trajectory.json 不是 skip"
    assert len(missing) == 1
    assert missing[0]["kind"] == "missing_atif"
    assert missing[0]["path"] == str(orphan.resolve())
    assert "缺少" in missing[0]["reason"]
    assert "NonZeroAgentExitCodeError" in missing[0]["reason"]
    assert "exception" not in missing[0], "报告只留定位信息，细节回原始目录查"


def test_case_discovery_ignores_directory_names(tmp_path: Path):
    """job 根与 trial 都用自定义名字（无 __），仍能正确发现。"""
    job = tmp_path / "my-run"
    _make_trial(job, name="trial-no-separator", reward="1.0")
    (job / "result.json").write_text(json.dumps({
        "id": "job-1", "n_total_trials": 1, "stats": {},
    }), encoding="utf-8")

    records, skipped, missing = scan.load_trials(job)

    assert [r.trial_dir.name for r in records] == ["trial-no-separator"]
    assert skipped == 0 and missing == []


def test_job_root_with_timestamp_name_is_not_case(tmp_path: Path):
    """harbor 默认 job 目录名含 __，但 job 级 result.json 不应被当成 trial。"""
    job = tmp_path / "2026-11-13__07-01-21"
    _make_trial(job, name="db-wal-recovery__ci3cG8H", reward="1.0")
    (job / "result.json").write_text(json.dumps({
        "id": "job-2", "n_total_trials": 1, "stats": {"n_completed_trials": 1},
    }), encoding="utf-8")

    records, skipped, missing = scan.load_trials(job)

    assert [r.trial_dir.name for r in records] == ["db-wal-recovery__ci3cG8H"]
    assert skipped == 0 and missing == []


def test_case_with_only_verifier_output_is_missing(tmp_path: Path):
    """只剩 verifier 产物的 trial 也要报 missing，不能静默消失。"""
    trial = tmp_path / "t-rewardonly"
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / "reward.txt").write_text("0.0", encoding="utf-8")

    records, skipped, missing = scan.load_trials(tmp_path)

    assert records == [] and skipped == 0
    assert len(missing) == 1 and missing[0]["kind"] == "missing_atif"


def test_nested_duplicate_case_counted_once(tmp_path: Path):
    """外层已是 case 时，内部嵌套的 case 目录不重复计入。"""
    _make_trial(tmp_path, name="outer", reward="1.0")
    _make_trial(tmp_path / "outer", name="inner", reward="1.0")

    records, _, missing = scan.load_trials(tmp_path)

    assert [r.trial_dir.name for r in records] == ["outer"]
    assert missing == []


def test_container_root_with_trial_schema_result_not_a_case(tmp_path: Path):
    """根目录 result.json 若变成 trial schema，不能把子 trial 整体吞掉。"""
    _make_trial(tmp_path, name="child-a", reward="1.0")
    _make_trial(tmp_path, name="child-b", reward="0.0")
    (tmp_path / "result.json").write_text(
        json.dumps({"trial_name": "child-a", "task_id": "child-a"}),
        encoding="utf-8")

    assert [p.name for p in scan.iter_case_dirs(tmp_path)] == ["child-a", "child-b"]


def test_trial_root_with_artifacts_is_still_a_case(tmp_path: Path):
    """单独导入一个 trial 目录时，根目录仍要被识别为 case。"""
    trial = _make_trial(tmp_path, name="only", reward="1.0")
    (trial / "result.json").write_text(
        json.dumps({"trial_name": "only", "task_id": "only"}), encoding="utf-8")

    assert list(scan.iter_case_dirs(trial)) == [trial]


def test_unreadable_result_without_artifacts_is_ignored(tmp_path: Path):
    """坏 result.json 且没有任何产物，无数据可导，不算 case。"""
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "result.json").write_text("{not json", encoding="utf-8")

    assert list(scan.iter_case_dirs(tmp_path)) == []


def test_trial_filter_by_reward(tmp_path: Path):
    _make_trial(tmp_path, name="ok__agent__model__1", reward="1.0")
    _make_trial(tmp_path, name="bad__agent__model__1", reward="0.0")

    success, _, _ = scan.load_trials(tmp_path, trial_filter="success")
    failure, _, _ = scan.load_trials(tmp_path, trial_filter="failure")
    assert [r.task_id for r in success] == ["ok"]
    assert [r.task_id for r in failure] == ["bad"]


# ---------------- 计划 → 真实 span 树 ----------------

def _memory_provider(ids):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider(id_generator=ids)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_execute_plan_replays_planned_ids(trial):
    from harborstack.convert.export import PlanIds, execute_plan

    plan = planmod.build_span_plan(trial)
    ids = PlanIds()
    provider, memory = _memory_provider(ids)
    ids.arm(plan[0].trace_id, [spec.span_id for spec in plan])

    info = execute_plan(provider.get_tracer("t"), plan)
    spans = memory.get_finished_spans()

    assert info["trace_id"] == f"{plan[0].trace_id:032x}"
    assert info["observation_id"] == f"{plan[0].span_id:016x}"
    assert len(spans) == len(plan)
    assert {span.context.trace_id for span in spans} == {plan[0].trace_id}
    assert {span.context.span_id for span in spans} == {spec.span_id for spec in plan}

    by_id = {span.context.span_id: span for span in spans}
    for idx, spec in enumerate(plan[1:], start=1):
        span = by_id[spec.span_id]
        expected_parent = plan[spec.parent].span_id
        assert span.parent.span_id == expected_parent, f"{spec.name} 挂错了父节点"

    provider.shutdown()


def test_execute_plan_is_idempotent_across_runs(trial, tmp_path: Path):
    """同一 Trial 连续两次上报，逐 span 的 (id, start) 完全一致。"""
    from harborstack.convert.export import PlanIds, execute_plan

    def run() -> list[tuple[int, int, int]]:
        plan = planmod.build_span_plan(trial)
        ids = PlanIds()
        provider, memory = _memory_provider(ids)
        ids.arm(plan[0].trace_id, [spec.span_id for spec in plan])
        execute_plan(provider.get_tracer("t"), plan)
        out = sorted((s.context.trace_id, s.context.span_id, s.start_time)
                     for s in memory.get_finished_spans())
        provider.shutdown()
        return out

    assert run() == run()


# ---------------- scores ----------------

def test_score_items_cover_reward_details_and_phenotypes(trial):
    from harborstack.convert import scores

    trial.reward_details = {"c1": 1.0}
    trial.phenotypes = ["tool_loop"]
    items = {item["name"]: item for item in scores.score_items(trial)}

    assert items["reward"]["value"] == 1.0
    assert items["reward.c1"]["dataType"] == "NUMERIC"
    assert items["phenotype"]["dataType"] == "CATEGORICAL"
    assert items["phenotype"]["value"] == "tool_loop"


def test_backfill_carries_trace_timestamp(monkeypatch, trial):
    """得分必须与 trace 用同一时间戳，否则按时间过滤时两者错位。"""
    from harborstack.convert import scores

    sent: list[dict] = []
    monkeypatch.setattr(scores, "_post_score",
                        lambda url, auth, payload, timeout: sent.append(payload) or (200, "ok"))
    trial.reward = 1.0
    trial.reward_details = {}
    trial.phenotypes = []
    ts = "2026-10-24T08:44:42.656000+00:00"

    scores.backfill("http://x", "pk", "sk", {"trace_id": "t1", "observation_id": "o1"},
                    trial, timestamp=ts)

    assert sent[0]["timestamp"] == ts
    assert sent[0]["traceId"] == "t1"
