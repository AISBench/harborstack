"""判分回填：走 Langfuse Public API，只依赖标准库。

``POST {host}/api/public/scores``，body 见 ``score_items``。
CATEGORICAL 分值（如 phenotype）若未在 UI 预先创建同名 score config，
部分版本会拒绝写入。
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from typing import Any

REWARD_SCORE = "reward"
PHENOTYPE_SCORE = "phenotype"


def score_id(trace_id: str, name: str) -> str:
    """由 trace 与得分名推导稳定 id，使重复导入覆盖而非追加。"""
    return hashlib.sha256(f"{trace_id}:{name}".encode("utf-8")).hexdigest()[:32]


def score_items(trial) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if trial.reward is not None:
        items.append({"name": REWARD_SCORE, "value": float(trial.reward),
                      "dataType": "NUMERIC"})
    for name, value in (trial.reward_details or {}).items():
        try:
            items.append({"name": f"{REWARD_SCORE}.{name}",
                          "value": float(value), "dataType": "NUMERIC"})
        except (TypeError, ValueError):
            continue
    for tag in trial.phenotypes or []:
        items.append({"name": PHENOTYPE_SCORE, "value": str(tag),
                      "dataType": "CATEGORICAL"})
    return items


def _post_score(url: str, auth: str, payload: dict[str, Any],
                timeout: float) -> tuple[int | None, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, "ok" if resp.status < 300 else resp.read()[:160].decode(
                "utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:160].decode("utf-8", "replace")
        return exc.code, detail
    except urllib.error.URLError as exc:
        return None, str(exc.reason)[:160]


def backfill(host: str, public_key: str, secret_key: str,
             ids: dict[str, str], trial, timestamp: str | None = None,
             timeout: float = 15.0
             ) -> list[tuple[str, int | None, str]]:
    """逐条写入判分；返回 [(score 名, HTTP 状态, 详情)]。

    ``timestamp`` 用 trace 的起始时间，保证得分与 trace 落在同一时间窗口；
    缺省时 Langfuse 会记为写入时刻，导致按时间过滤时两者不一致。
    """
    url = host.rstrip("/") + "/api/public/scores"
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    results: list[tuple[str, int | None, str]] = []
    for item in score_items(trial):
        payload = {"id": score_id(ids["trace_id"], str(item["name"])),
                   "traceId": ids["trace_id"],
                   "observationId": ids["observation_id"], **item}
        if timestamp:
            payload["timestamp"] = timestamp
        status, info = _post_score(url, auth, payload, timeout)
        results.append((str(item["name"]), status, info))
    return results
