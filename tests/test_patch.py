"""M2：RCpc 编码替换与补丁缓存的单元测试。"""

import hashlib
import json
from pathlib import Path

import pytest

from harborstack import lockfile
from harborstack.patch import elf, pipeline, rcpc

# as -march=armv8.3-a 汇编后 objdump 取得的真实编码
LDAPR_X = 0xF8BFC10E   # ldapr x14,[x8]
LDAPR_W = 0xB8BFC10E   # ldapr w14,[x8]
LDAPRB = 0x38BFC10E    # ldaprb w14,[x8]
LDAPRH = 0x78BFC10E    # ldaprh w14,[x8]
LDAR_X = 0xC8DFFD0E    # ldar x14,[x8]
LDAR_W = 0x88DFFD0E
LDARB = 0x08DFFD0E
LDARH = 0x48DFFD0E


def _blob(words, tail=b""):
    return b"".join(w.to_bytes(4, "little") for w in words) + tail


def _seg(n_words, offset=0, vaddr=0x1000):
    return elf.Segment(offset=offset, vaddr=vaddr, filesz=n_words * 4)


def test_scan_finds_all_four_forms():
    blob = _blob([LDAPR_X, LDAPR_W, LDAPRB, LDAPRH, LDAR_X, 0xD503201F])
    stats = rcpc.scan(blob, [_seg(6)])
    assert stats.total == 4
    assert stats.by_name == {"ldapr_x": 1, "ldapr_w": 1, "ldaprb": 1, "ldaprh": 1}
    assert stats.first_va == 0x1000


def test_apply_replaces_and_keeps_rt_rn_bits():
    buf = bytearray(_blob([LDAPR_X, LDAPR_W, LDAPRB, LDAPRH]))
    stats = rcpc.apply(buf, [_seg(4)])
    assert stats.total == 4
    words = [int.from_bytes(buf[i:i + 4], "little") for i in range(0, 16, 4)]
    assert words == [LDAR_X, LDAR_W, LDARB, LDARH]


def test_apply_leaves_unrelated_instructions():
    buf = bytearray(_blob([LDAR_X, 0xD503201F]))
    assert rcpc.apply(buf, [_seg(2)]).total == 0
    assert bytes(buf) == _blob([LDAR_X, 0xD503201F])


def test_apply_is_idempotent():
    buf = bytearray(_blob([LDAPR_X]))
    rcpc.apply(buf, [_seg(1)])
    assert rcpc.apply(buf, [_seg(1)]).total == 0
    assert rcpc.scan(bytes(buf), [_seg(1)]).total == 0


def test_scan_ignores_bytes_outside_segment():
    blob = _blob([LDAPR_X, LDAPR_X])
    seg = elf.Segment(offset=0, vaddr=0x2000, filesz=4)
    assert rcpc.scan(blob, [seg]).total == 1


def test_exec_segments_rejects_non_elf():
    with pytest.raises(elf.ElfError, match="ELF"):
        elf.exec_segments(b"not an elf file" + b"\0" * 64)


def _lock_with(tmp_path: Path, output_sha: str) -> lockfile.Lock:
    p = tmp_path / "lock.json"
    p.write_text(json.dumps({
        "schema": 1,
        "frozen_at": "test",
        "pins": {k: {"ref": "x", "digest": "sha256:" + "a" * 64} for k in
                 ("langfuse_web", "langfuse_worker", "clickhouse", "minio", "postgres", "redis")},
        "artifacts": {"clickhouse_patch": {
            "source_sha256": "b" * 64, "output_sha256": output_sha, "patcher": "1.0.0"}},
    }), encoding="utf-8")
    return lockfile.Lock.load(p)


def test_verify_cached_miss_when_missing(tmp_path):
    lock = _lock_with(tmp_path, "c" * 64)
    cfg = {"paths": {"cache_dir": str(tmp_path / "cache")}}
    assert pipeline.verify_cached(cfg, lock) is None


def test_verify_cached_hit_only_when_sha_matches(tmp_path):
    content = b"patched-binary"
    sha = hashlib.sha256(content).hexdigest()
    lock = _lock_with(tmp_path, sha)
    cfg = {"paths": {"cache_dir": str(tmp_path / "cache")}}

    bin_path, meta_path = pipeline.cache_paths(cfg, lock)
    bin_path.parent.mkdir(parents=True)
    bin_path.write_bytes(b"wrong-content")
    assert pipeline.verify_cached(cfg, lock) is None  # 存在但字节不符 -> miss

    bin_path.write_bytes(content)
    meta_path.write_text(json.dumps({"sites": {"total": 3}}), encoding="utf-8")
    hit = pipeline.verify_cached(cfg, lock)
    assert hit is not None and hit.cached and hit.sites == {"total": 3}


def test_cache_key_contains_digest_and_patcher(tmp_path):
    lock = _lock_with(tmp_path, "c" * 64)
    assert lock.patched_binary_stem() == "clickhouse-aaaaaaaaaaaa-p1.0.0"
