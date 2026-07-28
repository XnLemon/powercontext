"""Strict public domain models for the evaluation console."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from powercontext_eval.models import PowerContextRef
from powercontext_eval.runner import INSTANCE_ID


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FrozenModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class TaskPhase(StrEnum):
    PREPARING = "preparing"
    VALIDATING_GOLD = "validating_gold"
    RUNNING_OFF = "running_off"
    RUNNING_ON = "running_on"
    OFFICIAL_EVALUATION = "official_evaluation"
    GENERATING_REPORT = "generating_report"


class FailureCategory(StrEnum):
    INVALID_REQUEST = "invalid_request"
    QUEUE_UNAVAILABLE = "queue_unavailable"
    SOURCE_RESOLUTION = "source_resolution_failure"
    ENVIRONMENT_PREPARATION = "environment_preparation_failure"
    GOLD_VALIDATION = "gold_validation_failure"
    CODEX_EXECUTION = "codex_execution_failure"
    TREATMENT_VALIDATION = "treatment_validation_failure"
    OFFICIAL_EVALUATOR = "official_evaluator_failure"
    REPORT_GENERATION = "report_generation_failure"
    WORKER_INTERRUPTION = "worker_interruption"
    INTERNAL = "internal"


class TaskCreate(FrozenModel):
    powercontext_ref: str
    benchmark: Literal["swebench-pro"]
    instance_id: Literal["instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9"]
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


def _require_utc(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)):
        raise ValueError("Timestamps must use UTC")
    return value


class SafeFailure(FrozenModel):
    category: FailureCategory
    phase: TaskPhase | None = None
    summary: str = Field(min_length=1, max_length=500)


class TaskResult(FrozenModel):
    artifact_dir: str
    report_path: str
    off_resolved: bool
    on_resolved: bool


class TaskRecord(FrozenModel):
    task_id: str
    request: TaskCreate
    status: TaskStatus
    phase: TaskPhase | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    version: Annotated[int, Field(ge=0)] = 0
    failure_category: FailureCategory | None = None
    failure_phase: TaskPhase | None = None
    failure_summary: str | None = Field(default=None, max_length=500)
    result: TaskResult | None = None

    _utc_timestamps = field_validator("created_at", "started_at", "finished_at")(_require_utc)


class TaskSummary(FrozenModel):
    task_id: str
    powercontext_ref: str
    instance_id: str
    model: str
    status: TaskStatus
    phase: TaskPhase | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    version: Annotated[int, Field(ge=0)]
    off_resolved: bool | None = None
    on_resolved: bool | None = None

    _utc_timestamps = field_validator("created_at", "started_at", "finished_at")(_require_utc)


class TaskEvent(FrozenModel):
    task_id: str
    status: TaskStatus
    phase: TaskPhase | None = None
    version: Annotated[int, Field(ge=0)]
    occurred_at: datetime

    _utc_timestamp = field_validator("occurred_at")(_require_utc)


class Capabilities(FrozenModel):
    benchmarks: tuple[Literal["swebench-pro"], ...] = ("swebench-pro",)
    instances: tuple[Literal["instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9"], ...] = (INSTANCE_ID,)
    models: tuple[Literal["gpt-5.6-sol"], ...] = ("gpt-5.6-sol",)
    reasoning_efforts: tuple[Literal["medium"], ...] = ("medium",)
    treatment_modes: tuple[Literal["off_on"], ...] = ("off_on",)


class HealthResponse(FrozenModel):
    service: Literal["ok"]
    worker_lease_active: bool
    queued_tasks: Annotated[int, Field(ge=0)]
    running_tasks: Annotated[int, Field(ge=0)]


class MetricValue(FrozenModel):
    value: int | float | None


class ArmResponse(FrozenModel):
    arm: Literal["off", "on"]
    resolution: Literal["resolved", "unresolved"]
    input_tokens: int | None = None
    output_tokens: int | None = None
    elapsed_seconds: float | None = None
    patch_bytes: int | None = None


class MetricComparison(FrozenModel):
    off: int | float
    on: int | float
    delta: int | float
    percent: float | None


class ComparisonResponse(FrozenModel):
    input_tokens: MetricComparison | None = None
    output_tokens: MetricComparison | None = None
    elapsed_seconds: MetricComparison | None = None
    patch_bytes: MetricComparison | None = None


class TreatmentEvidence(FrozenModel):
    mcp_requests: Annotated[int, Field(ge=0)]
    prompt_sources: Annotated[int, Field(ge=0)]
    plugin_checkout_sha: str
    plugin_id: str
    plugin_installed: bool
    plugin_version: str
    scope_id: str
    server_ready: bool


class EvidenceResponse(FrozenModel):
    off: TreatmentEvidence
    on: TreatmentEvidence


class ReportResponse(FrozenModel):
    task_id: str
    acceptance_valid: bool
    off: ArmResponse
    on: ArmResponse
    comparison: ComparisonResponse
    evidence: EvidenceResponse
    revisions: dict[str, str]
    configuration: dict[str, str]
    generated_at: datetime

    _utc_timestamp = field_validator("generated_at")(_require_utc)

    @model_validator(mode="after")
    def require_distinct_arms(self) -> Self:
        if self.off.arm != "off" or self.on.arm != "on":
            raise ValueError("Report arms must preserve OFF/ON roles")
        return self
