"""ELF 解析：只取可执行 PT_LOAD 段，用于定位待扫描的指令区间。"""

from __future__ import annotations

import struct
from dataclasses import dataclass

PT_LOAD = 1
PF_X = 1


class ElfError(Exception):
    """不是可处理的 ELF 文件。"""


@dataclass(frozen=True)
class Segment:
    offset: int   # 文件偏移
    vaddr: int    # 虚拟地址
    filesz: int   # 文件内长度


def exec_segments(blob: bytes) -> list[Segment]:
    if len(blob) < 64 or blob[:4] != b"\x7fELF":
        raise ElfError("不是 ELF 文件")
    if blob[4] != 2:
        raise ElfError("只支持 ELF64")
    e_phoff = struct.unpack_from("<Q", blob, 0x20)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", blob, 0x36)
    if e_phentsize == 0 or e_phnum == 0:
        raise ElfError("没有 program header")

    segs: list[Segment] = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if off + 56 > len(blob):
            raise ElfError("program header 越界，文件可能被截断")
        p_type, p_flags = struct.unpack_from("<II", blob, off)
        p_offset, p_vaddr = struct.unpack_from("<QQ", blob, off + 8)
        p_filesz = struct.unpack_from("<Q", blob, off + 32)[0]
        if p_type == PT_LOAD and (p_flags & PF_X):
            segs.append(Segment(offset=p_offset, vaddr=p_vaddr, filesz=p_filesz))
    if not segs:
        raise ElfError("没有可执行段")
    return segs
