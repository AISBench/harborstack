"""ATIF 轨迹 → Langfuse span 的内嵌转换器。

分包职责：
- ``scan``   发现 Trial 目录、解析 trajectory.json 与 verifier 判分
- ``plan``   纯 Python 的 span 计划（含幂等 id 与时间收敛）
- ``export`` 把计划变成 OTel span 树并发往 Langfuse
- ``sanitize`` 内容脱敏
- ``scores`` 判分回填

``export`` 需要 OTel 依赖，故不在包初始化时导入；``sanitize`` 不在此重导出，
否则会遮蔽同名子模块。
"""

from .plan import SpanSpec, build_span_plan, detect_semantics, plan_stats
from .scan import (
    StepRecord,
    TrialRecord,
    iter_case_dirs,
    load_trial,
    load_trials,
    missing_entry,
)

__all__ = [
    "SpanSpec", "build_span_plan", "detect_semantics", "plan_stats",
    "StepRecord", "TrialRecord", "iter_case_dirs", "load_trial", "load_trials",
    "missing_entry",
]
