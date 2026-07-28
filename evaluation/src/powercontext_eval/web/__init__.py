"""Public domain types for the evaluation console."""

from powercontext_eval.web.config import WebConfig
from powercontext_eval.web.models import (
    Capabilities,
    FailureCategory,
    HealthResponse,
    ReportResponse,
    TaskCreate,
    TaskEvent,
    TaskPhase,
    TaskRecord,
    TaskStatus,
    TaskSummary,
)

__all__ = [
    "Capabilities",
    "FailureCategory",
    "HealthResponse",
    "ReportResponse",
    "TaskCreate",
    "TaskEvent",
    "TaskPhase",
    "TaskRecord",
    "TaskStatus",
    "TaskSummary",
    "WebConfig",
]
