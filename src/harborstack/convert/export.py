"""上报层：把 span 计划变成真实 OTel span 树并发往 Langfuse。

幂等 id 由计划给出（见 ``plan.trace_id_of`` / ``plan.span_id_of``），
这里用一个按计划顺序派发的 ``IdGenerator`` 把它们塞进 SDK，
因此同一 Trial 重复导入得到完全相同的 trace_id / span_id / start_time，
Langfuse 的 ``events_full`` 会按去重键收敛为一行。
"""

from __future__ import annotations

import base64
import json
import random
from typing import Any

from .plan import SpanSpec, parse_ts, to_ns

try:  # OTel 是上报层的硬依赖，缺失时给出可执行的提示
    from opentelemetry.sdk.trace import IdGenerator
except ImportError as exc:  # pragma: no cover - 环境缺依赖时的兜底
    raise ImportError(
        "缺少 OTel 依赖：uv pip install opentelemetry-sdk "
        "opentelemetry-exporter-otlp-proto-http"
    ) from exc


class ExportError(Exception):
    """缺少 API Key 或上报失败。"""


class PlanIds(IdGenerator):
    """按计划的 span 顺序派发 id；用完后回落到随机值（正常不会发生）。"""

    def __init__(self) -> None:
        self._trace_id = 0
        self._span_ids: list[int] = []

    def arm(self, trace_id: int, span_ids: list[int]) -> None:
        self._trace_id = trace_id
        self._span_ids = list(span_ids)

    def generate_trace_id(self) -> int:
        return self._trace_id

    def generate_span_id(self) -> int:
        if self._span_ids:
            return self._span_ids.pop(0)
        return random.getrandbits(63) or 1

    def is_trace_id_random(self) -> bool:
        return False


def _to_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """OTel 属性只接受原始类型或原始类型序列；
    dict/list（如 structural 脱敏结果）统一 JSON 序列化成字符串。"""

    def conv(value: Any) -> Any:
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        if isinstance(value, (list, tuple)) and all(
                isinstance(x, (str, bool, int, float)) for x in value):
            return list(value)
        return json.dumps(value, ensure_ascii=False)

    return {k: conv(v) for k, v in attrs.items() if v is not None}


def execute_plan(tracer, plan: list[SpanSpec]) -> dict[str, str]:
    """把计划变成 span 树并结束，返回十六进制的 trace_id / observation_id。"""
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import NonRecordingSpan, Status, StatusCode

    def mark_status(span, spec) -> None:
        """语言无关：只要计划里标了 ERROR，就同步 OTel 状态。"""
        if spec.attributes.get("langfuse.observation.level") != "ERROR":
            return
        span.set_status(Status(
            status_code=StatusCode.ERROR,
            description=str(
                spec.attributes.get("langfuse.observation.status_message") or ""),
        ))

    root_spec = plan[0]
    root = tracer.start_span(
        root_spec.name,
        attributes=_to_attrs(root_spec.attributes),
        start_time=to_ns(parse_ts(root_spec.start_ts)),
    )
    mark_status(root, root_spec)
    root.set_attribute(
        "langfuse.experiment.item.root_observation_id",
        format(root.get_span_context().span_id, "016x"),
    )
    for event in root_spec.events:
        root.add_event(
            event["name"],
            attributes=_to_attrs(event.get("attrs") or {}),
            timestamp=to_ns(parse_ts(event.get("ts"))),
        )

    contexts = {0: otel_trace.set_span_in_context(
        NonRecordingSpan(root.get_span_context()))}
    opened = []
    for idx, spec in enumerate(plan[1:], start=1):
        parent_ctx = contexts.get(spec.parent, contexts[0])
        span = tracer.start_span(
            spec.name,
            context=parent_ctx,
            attributes=_to_attrs(spec.attributes),
            start_time=to_ns(parse_ts(spec.start_ts)),
        )
        mark_status(span, spec)
        contexts[idx] = otel_trace.set_span_in_context(
            NonRecordingSpan(span.get_span_context()))
        opened.append((span, spec))

    for span, spec in reversed(opened):
        span.end(end_time=to_ns(parse_ts(spec.end_ts)))
    root.end(end_time=to_ns(parse_ts(root_spec.end_ts)))

    context = root.get_span_context()
    return {"trace_id": format(context.trace_id, "032x"),
            "observation_id": format(context.span_id, "016x")}


class Exporter:
    """绑定一个 Langfuse 实例；同一实例可连续上报多个 Trial。"""

    def __init__(self, host: str, public_key: str, secret_key: str,
                 environment: str = "experiment") -> None:
        if not public_key or not secret_key:
            raise ExportError("缺少 Langfuse API Key（pk-lf-... / sk-lf-...）")

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        self.host = host.rstrip("/")
        self.ids = PlanIds()
        auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self.provider = TracerProvider(
            resource=Resource.create({
                "service.name": "harborstack",
                "langfuse.environment": environment,
            }),
            id_generator=self.ids,
        )
        self.provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=self.host + "/api/public/otel/v1/traces",
            headers={
                "Authorization": f"Basic {auth}",
                # v4 数据模型实时入库；缺省可能延迟最长 10 分钟
                "x-langfuse-ingestion-version": "4",
            },
        )))
        self.tracer = self.provider.get_tracer("harborstack")

    def send(self, plan: list[SpanSpec]) -> dict[str, str]:
        if not plan:
            raise ExportError("空的 span 计划")
        self.ids.arm(plan[0].trace_id, [spec.span_id for spec in plan])
        return execute_plan(self.tracer, plan)

    def flush(self) -> None:
        self.provider.force_flush()

    def shutdown(self) -> None:
        try:
            self.provider.shutdown()
        except Exception:  # noqa: BLE001 - 关闭失败不影响已上报数据
            pass
