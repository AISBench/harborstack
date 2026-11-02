"""M5：run 的环境发现与参数拼装、display 的 report 结构与幂等 trace。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harborstack import cli
from harborstack import config as cfgmod
from harborstack.commands import display as display_cmd
from harborstack.commands import run as run_cmd

BASE = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


# ---------------- 构造 ----------------

def _iso(offset_s: float) -> str:
    return (BASE + timedelta(seconds=offset_s)).isoformat()


def _cfg(tmp_path: Path, repo: Path, var: Path) -> dict:
    toml = tmp_path / "harborstack.toml"
    toml.write_text(
        "[paths]\n"
        f'harbor_repo = "{repo}"\n'
        f'var_dir = "{var}"\n'
        "[report]\n"
        f'path = "{var}/report.json"\n',
        encoding="utf-8")
    return cfgmod.load_config(toml)


def _fake_repo(tmp_path: Path, body: str = "exit 0") -> Path:
    repo = tmp_path / "harbor"
    (repo / "src" / "harbor").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = 'harbor'\n", encoding="utf-8")
    exe = repo / ".venv" / "bin" / "harbor"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    exe.chmod(0o755)
    return repo


def _make_trial(root: Path, name: str = "task-a__openjiuwen__openai__1",
                reward: str | None = "1.0") -> Path:
    trial = root / name
    (trial / "agent").mkdir(parents=True)
    steps = [
        {"step_id": 1, "source": "user", "timestamp": _iso(0), "message": "solve it"},
        {"step_id": 2, "source": "agent", "timestamp": _iso(2),
         "model_name": "openai/deepseek-chat", "message": "step-2",
         "metrics": {"prompt_tokens": 10, "completion_tokens": 5,
                     "extra": {"total_latency_ms": 1000}},
         "tool_calls": [{"tool_call_id": "c1", "function_name": "bash",
                         "arguments": {"command": "ls"}}],
         "observation": {"results": [{"content": "ok"}]}},
        {"step_id": 3, "source": "agent", "timestamp": _iso(4),
         "model_name": "openai/deepseek-chat", "message": "step-3",
         "metrics": {"prompt_tokens": 10, "completion_tokens": 5,
                     "extra": {"total_latency_ms": 1000}},
         "tool_calls": [{"tool_call_id": "c2", "function_name": "read",
                         "arguments": {"path": "a"}}],
         "observation": {"results": [{"content": "ok"}]}},
    ]
    (trial / "agent" / "trajectory.json").write_text(json.dumps({
        "schema_version": "1.7",
        "session_id": f"session-{name}",
        "agent": {"name": "openjiuwen", "model_name": "openai/deepseek-chat"},
        "steps": steps,
        "final_metrics": {"total_steps": len(steps)},
    }), encoding="utf-8")
    if reward is not None:
        (trial / "verifier").mkdir()
        (trial / "verifier" / "reward.txt").write_text(reward, encoding="utf-8")
    return trial


@pytest.fixture()
def run_root(tmp_path: Path) -> Path:
    root = tmp_path / "run-1"
    _make_trial(root)
    return root


# ---------------- run ----------------

def test_new_run_id_is_sortable_and_unique():
    now = datetime(2026, 10, 28, 7, 3, 5)
    first, second = run_cmd.new_run_id(now), run_cmd.new_run_id(now)
    assert first.startswith("20261028-070305-")
    assert first != second


def test_harbor_prefix_prefers_repo_venv(tmp_path: Path):
    repo = _fake_repo(tmp_path)
    prefix, runner = run_cmd.harbor_prefix(repo)
    assert prefix == [str(repo / ".venv" / "bin" / "harbor")]
    assert runner == "repo-venv"


def test_harbor_prefix_falls_back_to_path_harbor(tmp_path: Path, monkeypatch):
    repo = tmp_path / "harbor"
    repo.mkdir()
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.setattr(run_cmd.shutil, "which", lambda name: "/usr/bin/harbor")
    prefix, runner = run_cmd.harbor_prefix(repo)
    assert prefix == ["/usr/bin/harbor"]
    assert runner == "global"


def test_harbor_prefix_refuses_to_install(tmp_path: Path, monkeypatch):
    repo = tmp_path / "harbor"
    repo.mkdir()
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.setattr(run_cmd.shutil, "which", lambda name: None)
    with pytest.raises(run_cmd.RunError, match="不负责安装依赖"):
        run_cmd.harbor_prefix(repo)


@pytest.mark.parametrize("args,expected", [
    ([], (False, None)),
    (["-o", "x"], (True, "x")),
    (["--jobs-dir", "y"], (True, "y")),
    (["--jobs-dir=z"], (True, "z")),
    (["--dataset", "d"], (False, None)),
])
def test_jobs_dir_flag_detection(args, expected):
    assert run_cmd.jobs_dir_flag(args) == expected


def test_build_command_injects_run_dir(tmp_path: Path):
    run_dir = tmp_path / "runs" / "r1"
    cmd, jobs = run_cmd.build_command(["harbor"], ["--dataset", "d"], run_dir)
    assert cmd == ["harbor", "--dataset", "d", "-o", str(run_dir)]
    assert jobs == run_dir


def test_build_command_keeps_user_jobs_dir(tmp_path: Path):
    run_dir = tmp_path / "runs" / "r1"
    cmd, jobs = run_cmd.build_command(["harbor"], ["-o", str(tmp_path / "mine")], run_dir)
    assert cmd == ["harbor", "-o", str(tmp_path / "mine")]
    assert jobs == (tmp_path / "mine").resolve()


def test_run_dry_run_executes_nothing(tmp_path: Path):
    repo = _fake_repo(tmp_path, body="touch SHOULD_NOT_EXIST")
    cfg = _cfg(tmp_path, repo, tmp_path / "var")
    result = run_cmd.run(cfg, ["--dataset", "d"], dry_run=True, log=lambda *_: None)

    assert result["ok"] and result["dry_run"]
    assert result["command"][0] == str(repo / ".venv" / "bin" / "harbor")
    assert result["command"][-2:] == ["-o", result["run_dir"]]
    assert not Path(result["run_dir"]).exists()
    assert not (repo / "SHOULD_NOT_EXIST").exists()


def test_run_records_meta(tmp_path: Path, monkeypatch):
    repo = _fake_repo(tmp_path)
    cfg = _cfg(tmp_path, repo, tmp_path / "var")
    monkeypatch.chdir(tmp_path)
    result = run_cmd.run(cfg, ["--dataset", "d"], log=lambda *_: None)

    assert result["ok"] and result["exit_code"] == 0
    meta = json.loads((Path(result["run_dir"]) / run_cmd.RUN_META).read_text(encoding="utf-8"))
    assert meta["argv"] == ["--dataset", "d"]
    assert meta["jobs_dir"] == result["run_dir"]
    assert meta["harbor"]["runner"] == "repo-venv"
    assert meta["exit_code"] == 0


def test_run_maps_nonzero_exit_to_data_error(tmp_path: Path, monkeypatch):
    repo = _fake_repo(tmp_path, body="exit 7")
    cfg = _cfg(tmp_path, repo, tmp_path / "var")
    monkeypatch.chdir(tmp_path)
    result = run_cmd.run(cfg, ["--dataset", "d"], log=lambda *_: None)
    assert not result["ok"]
    assert result["exit_code"] == 7
    assert result["mapped_exit"] == 5


def test_run_requires_harbor_args(tmp_path: Path):
    cfg = _cfg(tmp_path, _fake_repo(tmp_path), tmp_path / "var")
    with pytest.raises(run_cmd.RunError):
        run_cmd.run(cfg, [])
    assert cli.main(["run"]) == 2


def test_cli_run_dry_run_wiring(tmp_path: Path):
    repo = _fake_repo(tmp_path)
    cfg_path = tmp_path / "harborstack.toml"
    cfg_path.write_text(
        "[paths]\n"
        f'harbor_repo = "{repo}"\n'
        f'var_dir = "{tmp_path / "var"}"\n',
        encoding="utf-8")
    code = cli.main(["run", "--config", str(cfg_path), "--dry-run", "--", "--dataset", "d"])
    assert code == 0


# ---------------- display ----------------

@pytest.fixture
def healthy(monkeypatch):
    """display 强制走健康门禁；测试用桩替代真实 manifest 与 web 探测。"""
    monkeypatch.setattr(display_cmd, "check_health",
                        lambda cfg, timeout=10: [
                            {"id": "manifest", "status": "ok", "detail": "stub"},
                            {"id": "web", "status": "ok", "detail": "stub"}])


def _display_cfg(tmp_path: Path, run_root: Path, web: int = 9) -> dict:
    var = tmp_path / "var"
    var.mkdir(exist_ok=True)
    (var / "ports.json").write_text(json.dumps({"web": web}), encoding="utf-8")
    return _cfg(tmp_path, tmp_path / "harbor", var)


def test_display_dry_run_report_shape(tmp_path: Path, run_root: Path, healthy):
    cfg = _display_cfg(tmp_path, run_root)
    result = display_cmd.run_display(cfg, run_dir=str(run_root), dry_run=True,
                                     log=lambda *_: None)

    assert result["exit"] == 0
    report = result["report"]
    assert report["status"] == "ok" and report["dry_run"] is True
    assert report["run"]["trials"] == 1 and report["run"]["skipped"] == 0
    entry = report["trials"][0]
    assert entry["counts"] == {"events": 5, "generation": 2, "span": 3}
    assert entry["timing"]["semantics"] == "end"
    assert entry["timing"]["root_s"] == pytest.approx(4.0)
    assert entry["timing"]["llm_s"] == pytest.approx(2.0)
    assert entry["timing"]["tool_s"] == pytest.approx(1.0)
    assert entry["reconcile"]["llm_sum_match"] is True
    assert entry["reconcile"]["negative"] == 0
    assert entry["reconcile"]["outside_root"] == 0
    assert len(entry["trace"]["id"]) == 32
    assert entry["scores"] == []
    assert not (run_root / display_cmd.REPORT_NAME).exists(), "dry-run 不应落盘"


def test_display_report_lists_case_without_atif(tmp_path: Path, run_root: Path, healthy):
    orphan = run_root / "task-b__openjiuwen__openai__1"
    orphan.mkdir()
    (orphan / "result.json").write_text(json.dumps({
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": "container exited before agent wrote trajectory",
        },
    }), encoding="utf-8")

    cfg = _display_cfg(tmp_path, run_root)
    result = display_cmd.run_display(cfg, run_dir=str(run_root), dry_run=True,
                                     log=lambda *_: None)

    report = result["report"]
    assert report["run"]["trials"] == 1
    assert report["run"]["skipped"] == 0
    assert report["run"]["missing"] == 1
    entry = report["missing"][0]
    assert entry["trial"] == "task-b__openjiuwen__openai__1"
    assert entry["kind"] == "missing_atif"
    assert entry["path"] == str(orphan.resolve())
    assert "NonZeroAgentExitCodeError" in entry["reason"]
    assert result["exit"] == 0, "缺文件的 case 只进报告，不中断也不改变退出码"


def test_display_trace_ids_are_stable(tmp_path: Path, run_root: Path, healthy):
    cfg = _display_cfg(tmp_path, run_root)
    first = display_cmd.run_display(cfg, run_dir=str(run_root), dry_run=True,
                                    log=lambda *_: None)
    second = display_cmd.run_display(cfg, run_dir=str(run_root), dry_run=True,
                                     log=lambda *_: None)
    assert first["report"]["trials"][0]["trace"]["id"] == \
        second["report"]["trials"][0]["trace"]["id"]


def test_display_filters_by_reward(tmp_path: Path, run_root: Path, healthy):
    _make_trial(run_root, name="task-b__openjiuwen__openai__1", reward="0.0")
    cfg = _display_cfg(tmp_path, run_root)
    result = display_cmd.run_display(cfg, run_dir=str(run_root), dry_run=True,
                                     trial_filter="success",
                                     log=lambda *_: None)
    assert result["report"]["run"]["trials"] == 1
    assert result["report"]["trials"][0]["name"] == "task-a"


def test_display_health_failure_exits_4(tmp_path: Path, run_root: Path):
    cfg = _display_cfg(tmp_path, run_root)
    result = display_cmd.run_display(cfg, run_dir=str(run_root), dry_run=True,
                                     log=lambda *_: None)
    assert result["exit"] == 4
    assert result["report"] is None
    assert all(c["status"] != "ok" for c in result["checks"])


def test_display_empty_run_fails(tmp_path: Path, healthy):
    empty = tmp_path / "empty"
    empty.mkdir()
    cfg = _display_cfg(tmp_path, empty)
    result = display_cmd.run_display(cfg, run_dir=str(empty), dry_run=True,
                                     log=lambda *_: None)
    assert result["exit"] == 5
    assert result["report"]["status"] == "failed"


def test_write_report_writes_run_and_configured_paths(tmp_path: Path):
    cfg = _display_cfg(tmp_path, tmp_path / "run")
    report = {"schema": 1, "status": "ok", "trials": []}
    display_cmd._write_report(tmp_path / "run", cfg, report)

    for path in (tmp_path / "run" / display_cmd.REPORT_NAME,
                 cfgmod.expand_path(cfg["report"]["path"])):
        assert json.loads(path.read_text(encoding="utf-8")) == report


def test_display_llm_sum_not_applicable_when_semantics_ambiguous(tmp_path: Path, healthy):
    root = tmp_path / "run-amb"
    trial = _make_trial(root)
    traj = json.loads((trial / "agent" / "trajectory.json").read_text(encoding="utf-8"))
    traj["steps"][1]["metrics"]["extra"]["total_latency_ms"] = 2000
    (trial / "agent" / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")

    cfg = _display_cfg(tmp_path, root)
    result = display_cmd.run_display(cfg, run_dir=str(root), dry_run=True,
                                     log=lambda *_: None)
    entry = result["report"]["trials"][0]
    assert entry["timing"]["semantics"] == "ambiguous"
    assert entry["reconcile"]["llm_sum_match"] is None


def test_display_surfaces_agent_exception(tmp_path: Path, healthy):
    root = tmp_path / "run-exc"
    trial = _make_trial(root, reward="0.0")
    (trial / "result.json").write_text(json.dumps({
        "task_name": "terminal-bench/path-tracing-reverse",
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": "model call failed: Error code: 402 - Insufficient Balance",
        },
        "verifier_result": {"rewards": {"reward": 0.0}},
    }), encoding="utf-8")

    cfg = _display_cfg(tmp_path, root)
    result = display_cmd.run_display(cfg, run_dir=str(root), dry_run=True,
                                     log=lambda *_: None)

    entry = result["report"]["trials"][0]
    assert entry["status"] == "agent_error"
    assert entry["exception"]["exception_type"] == "NonZeroAgentExitCodeError"
    assert "402" in entry["error"]
    assert entry["counts"]["events"] > 0, "异常 Trial 仍应导入已有轨迹"
    assert result["report"]["status"] == "failed"
    assert result["exit"] == 5


def test_score_id_is_stable_per_trace_and_name():
    from harborstack.convert import scores as scoresmod

    first = scoresmod.score_id("a" * 32, "reward")
    assert first == scoresmod.score_id("a" * 32, "reward")
    assert len(first) == 32
    assert first != scoresmod.score_id("b" * 32, "reward")
    assert first != scoresmod.score_id("a" * 32, "reward.accuracy")


def test_tool_output_matches_source_call_id(tmp_path: Path):
    from harborstack.convert import plan as planmod
    from harborstack.convert import scan as scanmod

    root = tmp_path / "run-calls"
    trial = _make_trial(root, name="task-c__agent__model__1", reward="1.0")
    (trial / "agent" / "trajectory.json").write_text(json.dumps({
        "schema_version": "1.7",
        "session_id": "session-calls",
        "agent": {"name": "agent", "model_name": "m"},
        "steps": [
            {"step_id": 1, "source": "user", "timestamp": _iso(0), "message": "go"},
            {"step_id": 2, "source": "agent", "timestamp": _iso(2), "message": "",
             "model_name": "m",
             "tool_calls": [
                 {"tool_call_id": "c1", "function_name": "bash",
                  "arguments": {"command": "ls"}},
                 {"tool_call_id": "c2", "function_name": "bash",
                  "arguments": {"command": "pwd"}}],
             "observation": {"results": [
                 {"source_call_id": "c1", "content": "FILE-A"},
                 {"source_call_id": "c2", "content": "DIR-B"}]}},
        ],
    }), encoding="utf-8")

    rec = scanmod.load_trial(trial)
    plan = planmod.build_span_plan(rec, mode="full", run_name="run-calls")
    tools = [s for s in plan if s.role == "tool"]
    assert len(tools) == 2
    outputs = [s.attributes["langfuse.observation.output"] for s in tools]
    assert outputs == ["FILE-A", "DIR-B"], "每个 tool 只应拿到自己的 observation"


def test_tool_output_falls_back_when_call_id_missing(tmp_path: Path):
    from harborstack.convert import plan as planmod
    from harborstack.convert import scan as scanmod

    root = tmp_path / "run-fallback"
    trial = _make_trial(root, name="task-d__agent__model__1", reward="1.0")
    (trial / "agent" / "trajectory.json").write_text(json.dumps({
        "schema_version": "1.7",
        "session_id": "session-fallback",
        "agent": {"name": "agent", "model_name": "m"},
        "steps": [
            {"step_id": 1, "source": "user", "timestamp": _iso(0), "message": "go"},
            {"step_id": 2, "source": "agent", "timestamp": _iso(2), "message": "",
             "model_name": "m",
             "tool_calls": [{"tool_call_id": "c9", "function_name": "bash",
                             "arguments": {"command": "ls"}}],
             "observation": {"results": [{"content": "JOINED"}]}},
        ],
    }), encoding="utf-8")

    rec = scanmod.load_trial(trial)
    plan = planmod.build_span_plan(rec, mode="full", run_name="run-fallback")
    tool = next(s for s in plan if s.role == "tool")
    assert tool.attributes["langfuse.observation.output"] == "JOINED"


def test_trial_error_marks_root_and_last_generation(tmp_path: Path):
    from harborstack.convert import plan as planmod
    from harborstack.convert import scan as scanmod

    trial = _make_trial(tmp_path / "run-err", name="task-e__agent__model__1",
                        reward="0.0")
    (trial / "result.json").write_text(json.dumps({
        "trial_name": trial.name,
        "verifier_result": {"rewards": {"reward": 0.0}},
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": "model call failed: 402 Insufficient Balance",
            "exception_traceback": "Traceback ...",
        },
    }), encoding="utf-8")

    rec = scanmod.load_trial(trial)
    plan = planmod.build_span_plan(rec, mode="full", run_name="run-err")
    root = plan[0]
    assert root.attributes["langfuse.observation.level"] == "ERROR"
    assert root.attributes["atif.error_scope"] == "trial"
    assert root.attributes["atif.error_source"] == "harbor_result"
    assert root.attributes["error.type"] == "NonZeroAgentExitCodeError"
    assert "402 Insufficient Balance" in root.attributes[
        "langfuse.observation.status_message"]

    gens = [s for s in plan if s.role == "generation"]
    assert gens, "样本轨迹应至少有一个 generation"
    assert gens[-1].attributes["langfuse.observation.level"] == "ERROR"
    # 只有最后一个 generation 被标记，其余保持正常
    for gen in gens[:-1]:
        assert "langfuse.observation.level" not in gen.attributes


def test_success_trial_has_no_error_level(tmp_path: Path):
    from harborstack.convert import plan as planmod
    from harborstack.convert import scan as scanmod

    trial = _make_trial(tmp_path / "run-ok", name="task-f__agent__model__1",
                        reward="1.0")
    rec = scanmod.load_trial(trial)
    plan = planmod.build_span_plan(rec, mode="full", run_name="run-ok")
    for spec in plan:
        assert spec.attributes.get("langfuse.observation.level") != "ERROR"
        assert "error.type" not in spec.attributes


# ---------------- display 幂等 ----------------

class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_trace_exists_uses_v2_observations(monkeypatch):
    seen: list[str] = []

    def fake_urlopen(request, timeout=0):
        seen.append(request.full_url)
        return _FakeResponse(b'{"data": [{"id": "ef9b4294d07959af"}]}')

    monkeypatch.setattr(display_cmd.urllib.request, "urlopen", fake_urlopen)
    assert display_cmd.trace_exists("http://127.0.0.1:18930", "pk", "sk", "abc") is True
    assert seen and "/api/public/v2/observations" in seen[0]
    assert "traceId=abc" in seen[0]


def test_trace_exists_false_when_empty_or_error(monkeypatch):
    monkeypatch.setattr(display_cmd.urllib.request, "urlopen",
                        lambda request, timeout=0: _FakeResponse(b'{"data": []}'))
    assert display_cmd.trace_exists("http://h", "pk", "sk", "abc") is False

    def boom(request, timeout=0):
        raise display_cmd.urllib.error.URLError("refused")

    monkeypatch.setattr(display_cmd.urllib.request, "urlopen", boom)
    assert display_cmd.trace_exists("http://h", "pk", "sk", "abc") is False
