"""Public batch contracts and derived lifecycle state."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from powercontext_eval.models import PowerContextRef
from powercontext_eval.web.models import TaskStatus


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class BatchCreate(_FrozenModel):
    powercontext_ref: str
    benchmark: Literal["swebench-pro"]
    task_set: Literal["swebench-pro-public-v2"]
    model: Literal["gpt-5.6-sol"]
    reasoning_effort: Literal["medium"]
    treatment_mode: Literal["off_on"]
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")

    @field_validator("powercontext_ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        PowerContextRef.parse(value)
        if value != "latest" and not value.startswith("commit:"):
            raise ValueError("Web evaluations accept only latest or an exact commit")
        return value


class BatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PairCategory(StrEnum):
    OFF_FAIL_ON_PASS = "off_fail_on_pass"
    OFF_PASS_ON_FAIL = "off_pass_on_fail"
    BOTH_PASS = "both_pass"
    BOTH_FAIL = "both_fail"
    EXECUTION_FAILURE = "execution_failure"


class BatchRecord(_FrozenModel):
    batch_id: str
    request: BatchCreate
    total_tasks: Annotated[int, Field(ge=1)]
    status: BatchStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    resolved_powercontext_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def require_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)):
            raise ValueError("Timestamps must use UTC")
        return value


def derive_batch_status(statuses: tuple[TaskStatus, ...]) -> BatchStatus:
    """Derive one batch state from its complete child-state vector."""

    if not statuses:
        raise ValueError("A batch must contain at least one task")
    if all(status is TaskStatus.CANCELLED for status in statuses):
        return BatchStatus.CANCELLED
    terminal = {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.INTERRUPTED,
        TaskStatus.CANCELLED,
    }
    if all(status in terminal for status in statuses):
        return BatchStatus.COMPLETED
    if all(status is TaskStatus.QUEUED for status in statuses):
        return BatchStatus.QUEUED
    return BatchStatus.RUNNING
