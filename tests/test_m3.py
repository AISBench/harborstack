"""M3：端口分配器、渲染、资产指纹、manifest、bundle 校验。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harborstack import config as cfgmod
from harborstack import lockfile
from harborstack import manifest
from harborstack.commands import bundle as bundle_cmd
from harborstack.commands import down as down_cmd
from harborstack.commands import init as init_cmd
from harborstack.deploy import ALWAYS_FILES, SKIP_CHECKSUM_XML, all_assets, compose_files
from harborstack.deploy import ports as portsmod
from harborstack.deploy import render
from harborstack.env import docker

REPO = Path(__file__).resolve().parents[1]
LOCK = lockfile.Lock.load(REPO / "lock.json")


def _cfg(tmp_path: Path, **overrides) -> dict:
    cfg = cfgmod.load_config(REPO / "harborstack.toml", sets=[
        f"paths.var_dir={tmp_path / 'var'}",
        f"paths.cache_dir={tmp_path / 'cache'}",
    ])
    for key, value in overrides.items():
        cfg[key] = {**cfg.get(key, {}), **value}
    return cfg


# ---------------- 端口分配器 ----------------

@pytest.mark.parametrize("machine,rcpc,expected", [
    ("x86_64", True, False),
    ("x86_64", False, False),      # x86 上 rcpc 与否都与穿刺无关
    ("aarch64", True, False),      # ARMv8.3+ 自带 RCpc，不需要穿刺
    ("aarch64", False, True),      # Kunpeng 920 这一类才需要
])
def test_needs_patch_matrix(monkeypatch, machine, rcpc, expected):
    from harborstack.env import probe

    monkeypatch.setattr(probe, "machine", lambda: machine)
    monkeypatch.setattr(probe, "has_rcpc", lambda: rcpc)
    assert probe.needs_clickhouse_patch() is expected


def test_allocate_leaves_no_socket_behind(tmp_path: Path):
    """探测只做 bind + close，分配完成后端口必须重新空闲，不留下任何监听。"""
    from harborstack.env import probe

    cfg = _cfg(tmp_path, ports={"range": [18970, 18999], "preferred": {"web": 18970}})
    got = portsmod.allocate(cfg, saved={})

    assert len(got) == len(portsmod.ORDER)
    for port in got.values():
        assert probe.port_free(port), f"{port} 在探测后仍被占用"


def test_allocate_prefers_preferred_web_then_scans_from_range_start():
    cfg = _cfg(Path("/tmp"), ports={"range": [18900, 18999], "preferred": {"web": 18930}})
    got = portsmod.allocate(cfg, saved={}, is_free=lambda p: True)

    assert got["web"] == 18930
    assert got["worker"] == 18900
    assert len(set(got.values())) == len(portsmod.ORDER)


def test_allocate_skips_occupied_ports():
    cfg = _cfg(Path("/tmp"))
    taken = {18930, 18931}
    got = portsmod.allocate(cfg, saved={}, is_free=lambda p: p not in taken)

    assert got["web"] not in taken
    assert not (set(got.values()) & taken)


def test_allocate_reuses_saved_when_still_free():
    cfg = _cfg(Path("/tmp"))
    saved = {name: 18920 + i for i, name in enumerate(portsmod.ORDER)}
    got = portsmod.allocate(cfg, saved=saved, is_free=lambda p: True)

    assert got == saved


def test_allocate_raises_when_range_too_small():
    cfg = _cfg(Path("/tmp"), ports={"range": [18900, 18901], "preferred": {"web": 18900}})
    with pytest.raises(portsmod.PortError):
        portsmod.allocate(cfg, saved={}, is_free=lambda p: True)


# ---------------- 渲染 ----------------

def test_write_env_is_private_and_contains_all_service_ports(tmp_path: Path):
    cfg = _cfg(tmp_path)
    allocated = {name: 18900 + i for i, name in enumerate(portsmod.ORDER)}
    sec = render.load_secrets(cfg, tmp_path / "var")

    env = render.build_env(cfg, LOCK, allocated, sec, None, SKIP_CHECKSUM_XML)
    path = render.write_env(tmp_path / "var", env)

    assert (path.stat().st_mode & 0o777) == 0o600
    for name, key in portsmod.ENV_KEYS.items():
        assert env[key] == str(allocated[name])
    assert env["NEXTAUTH_URL"] == f"http://localhost:{allocated['web']}"
    assert env["HS_IMAGE_LANGFUSE"].endswith("langfuse/langfuse:4")
    assert "HS_CLICKHOUSE_BIN" not in env  # 未穿刺时不应挂载二进制


def test_patched_env_passes_binary_path(tmp_path: Path):
    cfg = _cfg(tmp_path)
    allocated = {name: 18900 + i for i, name in enumerate(portsmod.ORDER)}
    sec = render.load_secrets(cfg, tmp_path / "var")
    bin_path = tmp_path / "clickhouse.bin"

    env = render.build_env(cfg, LOCK, allocated, sec, bin_path, SKIP_CHECKSUM_XML)
    assert env["HS_CLICKHOUSE_BIN"] == str(bin_path)


def test_generated_secrets_are_stable_across_calls(tmp_path: Path):
    cfg = _cfg(tmp_path, langfuse={"security_mode": "generated"})
    first = render.load_secrets(cfg, tmp_path / "var")
    second = render.load_secrets(cfg, tmp_path / "var")

    assert first == second
    assert first["public_key"].startswith("pk-lf-")
    assert len(first["encryption_key"]) == 64


def test_default_secrets_do_not_depend_on_disk(tmp_path: Path):
    cfg = _cfg(tmp_path)
    values = render.load_secrets(cfg, tmp_path / "var")
    assert values == render.DEFAULT_SECRETS


def test_secrets_will_regenerate_only_when_state_is_lost(tmp_path: Path):
    cfg = _cfg(tmp_path, langfuse={"security_mode": "generated"})
    var = tmp_path / "var"

    assert render.secrets_will_regenerate(cfg, var) is True, "首次 init 没有密钥"

    render.load_secrets(cfg, var)
    assert render.secrets_will_regenerate(cfg, var) is False, "已有密钥应复用"

    other = _cfg(tmp_path, langfuse={"security_mode": "default"})
    assert render.secrets_will_regenerate(other, var) is True, "mode 变更会重建"

    (var / render.SECRETS_NAME).write_text("{broken", encoding="utf-8")
    assert render.secrets_will_regenerate(cfg, var) is True, "损坏的密钥会重建"


def test_init_refuses_existing_volumes_with_lost_secrets(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, langfuse={"security_mode": "generated"})
    monkeypatch.setattr(init_cmd, "_platform_info", lambda: {
        "machine": "x86_64", "rcpc": False, "needs_patch": False})
    monkeypatch.setattr(docker, "project_volumes", lambda project: [
        f"{project}_langfuse_postgres_data", f"{project}_langfuse_clickhouse_data"])

    with pytest.raises(init_cmd.InitError, match="secrets.json"):
        init_cmd.run_init(cfg, LOCK, log=lambda *_: None)


def test_init_allows_fresh_start_when_no_volumes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, langfuse={"security_mode": "generated"})
    monkeypatch.setattr(init_cmd, "_platform_info", lambda: {
        "machine": "x86_64", "rcpc": False, "needs_patch": False})
    monkeypatch.setattr(docker, "project_volumes", lambda project: [])
    # 走到安装步骤即证明卷校验放行；安装用假结果短路
    monkeypatch.setattr(init_cmd.install_cmd, "run_install", lambda *a, **kw: {
        "ok": False, "actions": [{"id": "image", "status": "fail", "detail": "stub"}]})

    result = init_cmd.run_init(cfg, LOCK, log=lambda *_: None)
    assert result["ok"] is False
    assert any(s["id"] == "volumes" and s["status"] == "ok" for s in result["steps"])


# ---------------- 资产与 manifest ----------------

def test_compose_assets_match_lock():
    assert LOCK.compose, "lock.json 缺少 compose.files"
    for path in all_assets():
        from harborstack.fsutil import sha256_file
        assert sha256_file(path) == LOCK.compose[path.name], path.name


def test_patch_layer_only_when_needed():
    assert compose_files(False) == ALWAYS_FILES
    assert len(compose_files(True)) == len(ALWAYS_FILES) + 1


def test_manifest_roundtrip(tmp_path: Path):
    cfg = _cfg(tmp_path)
    allocated = {name: 18900 + i for i, name in enumerate(portsmod.ORDER)}
    data = manifest.build(cfg, LOCK, {"machine": "aarch64", "rcpc": False,
                                     "needs_patch": True}, allocated, None,
                          manifest.PREPARED)
    path = manifest.save(manifest.path_for(cfg), data)

    loaded = manifest.load(path)
    assert loaded is not None
    assert loaded["state"] == "prepared"
    assert loaded["ports"] == allocated
    assert set(loaded["compose"]) == {p.name for p in all_assets()}


# ---------------- down 的隔离红线 ----------------

def test_down_refuses_foreign_project(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(render, "project_name", lambda _cfg: "someone-else")
    with pytest.raises(down_cmd.DownError):
        down_cmd.run_down(cfg, LOCK)


def test_down_is_noop_without_env_file(tmp_path: Path):
    cfg = _cfg(tmp_path)
    result = down_cmd.run_down(cfg, LOCK)
    assert result["ok"] is True
    assert "没有由 harborstack 启动的栈" in result["detail"]


# ---------------- bundle 校验 ----------------

def _fake_bundle(root: Path, tamper: bool = False) -> Path:
    (root / "compose").mkdir(parents=True)
    payload = root / "compose" / "a.yml"
    payload.write_text("services: {}\n", encoding="utf-8")
    from harborstack.fsutil import sha256_file
    files = [{"path": "compose/a.yml", "size": payload.stat().st_size,
              "sha256": sha256_file(payload)}]
    (root / "manifest.json").write_text(json.dumps({
        "schema": 1, "mode": "skeleton", "files": files,
    }), encoding="utf-8")
    if tamper:
        payload.write_text("services: {tampered: true}\n", encoding="utf-8")
    return root


def test_bundle_verify_accepts_intact(tmp_path: Path):
    verdict = bundle_cmd.verify(_fake_bundle(tmp_path / "b"))
    assert verdict["ok"] is True


def test_bundle_verify_detects_tamper(tmp_path: Path):
    verdict = bundle_cmd.verify(_fake_bundle(tmp_path / "b", tamper=True))
    assert verdict["ok"] is False
    assert verdict["mismatch"] == ["compose/a.yml"]


def test_bundle_verify_detects_missing(tmp_path: Path):
    root = _fake_bundle(tmp_path / "b")
    (root / "compose" / "a.yml").unlink()
    verdict = bundle_cmd.verify(root)
    assert verdict["ok"] is False
    assert verdict["missing"] == ["compose/a.yml"]


def test_pull_by_digest_pins_local_tag(monkeypatch):
    from harborstack.commands import install

    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    action = install._pull("docker.1ms.run/library/postgres:16", "sha256:abc")

    assert action.status == "ok"
    assert calls[0] == ["docker", "pull",
                        "docker.1ms.run/library/postgres:16@sha256:abc"]
    # 关键断言：拉取后必须把 tag 指回该 digest，否则 compose 会按 tag 重新解析
    assert calls[1] == ["docker", "tag",
                        "docker.1ms.run/library/postgres:16@sha256:abc",
                        "docker.1ms.run/library/postgres:16"]


def test_pull_raises_when_tag_fails(monkeypatch):
    from harborstack.commands import install

    class _Proc:
        def __init__(self, rc):
            self.returncode = rc
            self.stderr = "boom"

    def fake_run(cmd, **kwargs):
        return _Proc(0) if cmd[1] == "pull" else _Proc(1)

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    try:
        install._pull("ref/img:1", "sha256:abc")
    except install.InstallError as exc:
        assert "标记镜像失败" in str(exc)
    else:
        raise AssertionError("tag 失败时应抛 InstallError")


def test_pull_timeout_falls_back_to_tag(monkeypatch):
    """digest 路径超时不应直接抛出，必须继续尝试 tag 路径。"""
    from harborstack.commands import install

    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[2].endswith("@sha256:abc"):
            raise install.subprocess.TimeoutExpired(cmd, install.PULL_TIMEOUT)
        return _Proc()

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    monkeypatch.setattr(install.docker, "image_digest", lambda ref: "sha256:abc")

    action = install._pull("docker.1ms.run/library/postgres:16", "sha256:abc")

    assert action.status == "ok"
    assert calls[1][-1] == "docker.1ms.run/library/postgres:16"
    assert not any(c[1] == "tag" for c in calls), "tag 路径成功后无需再补 tag"


def test_pull_timeout_on_both_paths_mentions_mirror(monkeypatch):
    from harborstack.commands import install

    def fake_run(cmd, **kwargs):
        raise install.subprocess.TimeoutExpired(cmd, install.PULL_TIMEOUT)

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    with pytest.raises(install.InstallError, match="换源"):
        install._pull("docker.1ms.run/library/postgres:16", "sha256:abc")


def test_ensure_images_continues_after_single_failure(tmp_path: Path, monkeypatch):
    """单个镜像失败要记 fail 并继续，不能中断整批。"""
    from harborstack.commands import install

    class _Pin:
        def __init__(self, digest: str):
            self.digest = digest

    class _Lock:
        pins = {"postgres": _Pin("sha256:p"), "redis": _Pin("sha256:r")}

    cfg = _cfg(tmp_path, image={"mirror": "docker.1ms.run"})
    monkeypatch.setattr(install.docker, "image_digest", lambda ref: None)

    def fake_pull(ref: str, digest: str):
        if "postgres" in ref:
            raise install.InstallError("boom")
        return install.Action(ref, "ok", "ok")

    monkeypatch.setattr(install, "_pull", fake_pull)

    actions = install.ensure_images(cfg, _Lock(), dry_run=False)

    assert [a.status for a in actions] == ["fail", "ok"]
    assert "boom" in actions[0].detail
