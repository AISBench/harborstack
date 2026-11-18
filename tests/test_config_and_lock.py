"""配置优先级、lock 校验、doctor 在缺镜像时的行为。"""

import json
from pathlib import Path

import pytest

from harborstack import config as cfgmod
from harborstack import lockfile

REPO = Path(__file__).resolve().parents[1]


def test_toml_overrides_builtin_and_env_overrides_toml(tmp_path):
    (tmp_path / "harborstack.toml").write_text(
        "[ports]\npreferred = { web = 18951 }\n", encoding="utf-8")
    cfg = cfgmod.load_config(tmp_path / "harborstack.toml",
                             env={"HARBORSTACK_PORTS__PREFERRED__WEB": "18952"})
    assert cfg["ports"]["preferred"]["web"] == 18952
    assert cfg["ports"]["range"] == [18900, 18999]


def test_cli_set_has_highest_priority(tmp_path):
    cfg = cfgmod.load_config(None, sets=["ports.preferred.web=18953"],
                             env={"HARBORSTACK_PORTS__PREFERRED__WEB": "18952"})
    assert cfg["ports"]["preferred"]["web"] == 18953


def test_invalid_range_rejected(tmp_path):
    with pytest.raises(cfgmod.ConfigError, match="ports.range"):
        cfgmod.load_config(None, sets=["ports.range=[19010,19000]"])


def test_invalid_security_mode_rejected():
    with pytest.raises(cfgmod.ConfigError, match="security_mode"):
        cfgmod.load_config(None, sets=["langfuse.security_mode=nope"])


def _write_lock(tmp_path, mutate=None):
    data = json.loads((REPO / "lock.json").read_text())
    if mutate:
        mutate(data)
    p = tmp_path / "lock.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_lock_loads_repo_default():
    lock = lockfile.Lock.load(REPO / "lock.json")
    assert lock.pin("clickhouse").version == "25.12.11.4"
    assert lock.patched_binary_stem().startswith("clickhouse-7d93fb39bb2b-p")


def test_lock_rejects_bad_digest(tmp_path):
    p = _write_lock(tmp_path, lambda d: d["pins"]["clickhouse"].update(digest="sha256:xyz"))
    with pytest.raises(lockfile.LockError, match="digest"):
        lockfile.Lock.load(p)


def test_lock_rejects_missing_pin(tmp_path):
    p = _write_lock(tmp_path, lambda d: d["pins"].pop("redis"))
    with pytest.raises(lockfile.LockError, match="redis"):
        lockfile.Lock.load(p)


def test_digest_matches_handles_repo_digest_form():
    d = "sha256:" + "a" * 64
    assert lockfile.digest_matches(d, "docker.1ms.run/langfuse/langfuse@" + d)
    assert not lockfile.digest_matches(d, None)
