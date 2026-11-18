"""aarch64 RCpc 指令替换：LDAPR* -> LDAR*。

LDAR 是 RCsc 语义的 acquire-load，强于 LDAPR 的 RCpc，替换只加强内存序，
不会放宽任何约束，因此正确性安全（代价是极小的性能损失）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from harborstack.patch.elf import Segment

MASK = 0xFFFFFC00
RT_MASK = 0x3FF  # 低 10 位：Rt | Rn<<5，原样保留

# 原基址 -> (新基址, 名称)
PAIRS: dict[int, tuple[int, str]] = {
    0x38BFC000: (0x08DFFC00, "ldaprb"),
    0x78BFC000: (0x48DFFC00, "ldaprh"),
    0xB8BFC000: (0x88DFFC00, "ldapr_w"),
    0xF8BFC000: (0xC8DFFC00, "ldapr_x"),
}
LDAR_BASES = {new for new, _ in PAIRS.values()}


class PatchError(Exception):
    """穿刺后仍有残留，或编码不在预期集合内。"""


@dataclass
class PatchStats:
    total: int = 0
    by_name: dict[str, int] = field(default_factory=dict)
    first_va: int | None = None

    def add(self, name: str, va: int | None = None) -> None:
        self.total += 1
        self.by_name[name] = self.by_name.get(name, 0) + 1
        if self.first_va is None:
            self.first_va = va

    def as_dict(self) -> dict:
        return {"total": self.total, "by_name": dict(sorted(self.by_name.items())),
                "first_va": self.first_va}


def _word_offsets(blob_len: int, seg: Segment):
    """逐个 4 字节对齐扫描，避免跨段或越界。"""
    start = seg.offset + (-seg.offset % 4)
    end = min(blob_len, seg.offset + seg.filesz)
    return range(start, end - 3, 4)


def scan(blob: bytes, segments: list[Segment]) -> PatchStats:
    stats = PatchStats()
    for seg in segments:
        for off in _word_offsets(len(blob), seg):
            key = struct.unpack_from("<I", blob, off)[0] & MASK
            if key in PAIRS:
                stats.add(PAIRS[key][1], seg.vaddr + (off - seg.offset))
    return stats


def apply(blob: bytearray, segments: list[Segment]) -> PatchStats:
    stats = PatchStats()
    for seg in segments:
        for off in _word_offsets(len(blob), seg):
            word = struct.unpack_from("<I", blob, off)[0]
            key = word & MASK
            if key not in PAIRS:
                continue
            new = PAIRS[key][0] | (word & RT_MASK)
            if (new & MASK) not in LDAR_BASES:
                raise PatchError(f"替换结果不在 LDAR 集合内：{word:#010x} -> {new:#010x}")
            struct.pack_into("<I", blob, off, new)
            stats.add(PAIRS[key][1], seg.vaddr + (off - seg.offset))
    return stats
