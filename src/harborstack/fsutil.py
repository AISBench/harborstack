"""文件级工具：大文件分块哈希。"""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path, chunk: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()
