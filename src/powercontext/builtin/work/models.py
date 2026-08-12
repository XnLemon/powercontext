"""Typed records for the human and Agent work-continuity loop."""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.experience import TASK_OUTCOME_SOURCE_KIND
from powercontext.builtin.artifacts.handoff import (
    HandoffCitation,
    HandoffDisposition,
    HandoffResolution,
    HandoffResolutionSelection,
    PreparedHandoff,
)
from powercontext.builtin.artifacts.handoff.models import MAX_HANDOFF_CITATIONS, MAX_HANDOFF_STATE_STATEMENTS
from powercontext.sources import SourceRef

MAX_WORK_TEXT_LENGTH = 8_192
MAX_WORK_ITEMS = 64
MAX_WORK_EVIDENCE = 32
MAX_WORK_CLAIM_EVIDENCE = MAX_HANDOFF_CITATIONS - 1
MAX_HANDOFF_RECEIPT_EVIDENCE = (MAX_HANDOFF_STATE_STATEMENTS + 1) * MAX_HANDOFF_CITATIONS

WORK_CONTRACT_SOURCE_KIND = "work-contract"
HANDOFF_BOUNDARY_SOURCE_KIND = "handoff-boundary"
HANDOFF_RECEIPT_SOURCE_KIND = "handoff-receipt"

WorkClaimBasis: TypeAlias = Literal["declared", "verified"]
TaskOutcomeStatus: TypeAlias = Literal["succeeded", "partial", "blocked", "failed", "cancelled", "unknown"]
TaskCheckStatus: TypeAlias = Literal[
    "passed",
    "failed",
    "skipped",
    "timed_out",
    "unavailable",
    "cancelled",
    "unknown",
]
HandoffReceiptStatus: TypeAlias = Literal["accepted", "needs_clarification", "declined"]
ReceiptEvidenceStatus: TypeAlias = Literal["available", "unavailable"]
WorkSourceKind: TypeAlias = Literal["work-contract", "handoff-boundary", "handoff-receipt", "task-outcome"]


class _WorkValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WorkClaim(_WorkValue):
    """One producer claim, optionally grounded in exact PowerContext evidence."""

    text: Annotated[str, Field(max_length=MAX_WORK_TEXT_LENGTH)]
    basis: WorkClaimBasis = "declared"
    evidence: Annotated[tuple[HandoffCitation, ...], Field(max_length=MAX_WORK_CLAIM_EVIDENCE)] = ()

    @field_validator("text")
    @classmethod
    def require_text(cls, value: str) -> str:
        return _require_text("claim", value)

    @model_validator(mode="after")
    def require_evidence_for_verified_claims(self) -> WorkClaim:
        if self.basis == "verified" and not self.evidence:
            raise ValueError("verified Work claims require exact evidence")  # noqa: TRY003
        if self.basis == "declared" and self.evidence:
            raise ValueError("declared Work claims cannot present evidence as verified")  # noqa: TRY003
        return self


class WorkContract(_WorkValue):
    """A concise, inspectable baseline for newly delegated work."""

    schema_version: Literal["powercontext.work-contract.v1"] = Field(
        default="powercontext.work-contract.v1",
        alias="schema",
    )
    trust: Literal["untrusted_input"] = "untrusted_input"
    objective: Annotated[str, Field(max_length=MAX_WORK_TEXT_LENGTH)]
    facts: Annotated[tuple[WorkClaim, ...], Field(max_length=MAX_WORK_ITEMS)] = ()
    in_scope: Annotated[tuple[str, ...], Field(min_length=1, max_length=MAX_WORK_ITEMS)]
    exclusions: Annotated[tuple[str, ...], Field(max_length=MAX_WORK_ITEMS)] = ()
    completion_criteria: Annotated[tuple[str, ...], Field(min_length=1, max_length=MAX_WORK_ITEMS)]
    authorization_notes: Annotated[tuple[str, ...], Field(max_length=MAX_WORK_ITEMS)] = ()
    open_questions: Annotated[tuple[str, ...], Field(max_length=MAX_WORK_ITEMS)] = ()

    @field_validator("objective")
    @classmethod
    def require_objective(cls, value: str) -> str:
        return _require_text("objective", value)

    @field_validator("in_scope", "exclusions", "completion_criteria", "authorization_notes", "open_questions")
    @classmethod
    def require_text_items(cls, values: tuple[str, ...], info) -> tuple[str, ...]:
        return _require_text_items(info.field_name, values)


class TaskCheck(_WorkValue):
    """One check result whose uncertainty is preserved exactly."""

    name: Annotated[str, Field(max_length=MAX_WORK_TEXT_LENGTH)]
    status: TaskCheckStatus
    details: Annotated[str, Field(max_length=MAX_WORK_TEXT_LENGTH)] | None = None
    basis: WorkClaimBasis = "declared"
    evidence: Annotated[tuple[HandoffCitation, ...], Field(max_length=MAX_WORK_EVIDENCE)] = ()

    @field_validator("name", "details")
    @classmethod
    def require_text(cls, value: str | None, info) -> str | None:
        return None if value is None else _require_text(info.field_name, value)

    @model_validator(mode="after")
    def require_evidence_for_verified_checks(self) -> TaskCheck:
        if self.basis == "verified" and not self.evidence:
            raise ValueError("verified Task checks require exact evidence")  # noqa: TRY003
        if self.basis == "declared" and self.evidence:
            raise ValueError("declared Task checks cannot present evidence as verified")  # noqa: TRY003
        return self


class TaskOutcome(_WorkValue):
    """What one completed or interrupted work attempt actually produced."""

    schema_version: Literal["powercontext.task-outcome.v1"] = Field(
        default="powercontext.task-outcome.v1",
        alias="schema",
    )
    trust: Literal["untrusted_observation"] = "untrusted_observation"
    objective: Annotated[str, Field(max_length=MAX_WORK_TEXT_LENGTH)]
    status: TaskOutcomeStatus
    summary: Annotated[str, Field(max_length=MAX_WORK_TEXT_LENGTH)]
    observations: Annotated[tuple[WorkClaim, ...], Field(min_length=1, max_length=MAX_WORK_ITEMS)]
    checks: Annotated[tuple[TaskCheck, ...], Field(max_length=MAX_WORK_ITEMS)] = ()
    produced_artifacts: Annotated[tuple[ArtifactRef, ...], Field(max_length=MAX_WORK_EVIDENCE)] = ()
    remaining_work: Annotated[tuple[str, ...], Field(max_length=MAX_WORK_ITEMS)] = ()

    @field_validator("objective", "summary")
    @classmethod
    def require_text(cls, value: str, info) -> str:
        return _require_text(info.field_name, value)

    @field_validator("remaining_work")
    @classmethod
    def require_remaining_work(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_text_items("remaining_work", values)


class CurrentWorkHandoff(_WorkValue):
    """Caller-inspected current state used to prepare a temporary Handoff."""

    schema_version: Literal["powercontext.current-work-handoff.v1"] = Field(
        default="powercontext.current-work-handoff.v1",
        alias="schema",
    )
    trust: Literal["untrusted_input"] = "untrusted_input"
    objective: Annotated[str, Field(max_length=MAX_WORK_TEXT_LENGTH)]
    state: Annotated[tuple[WorkClaim, ...], Field(min_length=1, max_length=MAX_WORK_ITEMS)]
    disposition: HandoffDisposition
    next_action: WorkClaim | None = None
    omissions: Annotated[tuple[str, ...], Field(max_length=MAX_WORK_ITEMS)] = ()

    @field_validator("objective")
    @classmethod
    def require_objective(cls, value: str) -> str:
        return _require_text("objective", value)

    @field_validator("omissions")
    @classmethod
    def require_omissions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_text_items("omissions", values)


class CreateWorkContract(_WorkValue):
    source_id: Annotated[str, Field(max_length=256)]
    contract: WorkContract

    @field_validator("source_id")
    @classmethod
    def require_source_id(cls, value: str) -> str:
        return _require_text("source_id", value)


class RecordTaskOutcome(_WorkValue):
    source_id: Annotated[str, Field(max_length=256)]
    outcome: TaskOutcome

    @field_validator("source_id")
    @classmethod
    def require_source_id(cls, value: str) -> str:
        return _require_text("source_id", value)


class HandoffCurrentWork(_WorkValue):
    source_id: Annotated[str, Field(max_length=256)]
    handoff: CurrentWorkHandoff

    @field_validator("source_id")
    @classmethod
    def require_source_id(cls, value: str) -> str:
        return _require_text("source_id", value)


class AcknowledgeHandoff(_WorkValue):
    source_id: Annotated[str, Field(max_length=256)]
    receiver: Annotated[str, Field(max_length=256)]
    status: HandoffReceiptStatus
    selection: HandoffResolutionSelection
    prepared: PreparedHandoff | None = None
    revision: ArtifactRef | None = None
    message: Annotated[str, Field(max_length=MAX_WORK_TEXT_LENGTH)] | None = None

    @field_validator("source_id", "receiver", "message")
    @classmethod
    def require_text(cls, value: str | None, info) -> str | None:
        return None if value is None else _require_text(info.field_name, value)

    @model_validator(mode="after")
    def validate_selection_and_message(self) -> AcknowledgeHandoff:
        if self.selection == "prepared":
            valid_selection = self.prepared is not None and self.revision is None
        elif self.selection == "exact":
            valid_selection = self.prepared is None and self.revision is not None
        else:
            valid_selection = self.prepared is None and self.revision is None
        if not valid_selection:
            raise ValueError("Handoff acknowledgement selection does not match its exact input")  # noqa: TRY003
        if self.status != "accepted" and self.message is None:
            raise ValueError("non-accepted Handoff acknowledgement requires a message")  # noqa: TRY003
        return self


class HandoffReceipt(_WorkValue):
    """The receiving participant's bounded acknowledgement of one resolved Handoff."""

    schema_version: Literal["powercontext.handoff-receipt.v1"] = Field(
        default="powercontext.handoff-receipt.v1",
        alias="schema",
    )
    trust: Literal["untrusted_observation"] = "untrusted_observation"
    receiver: str
    status: HandoffReceiptStatus
    selection: HandoffResolutionSelection
    selected_revision: ArtifactRef | None = None
    prepared_digest: str | None = None
    evidence_status: ReceiptEvidenceStatus
    unavailable_evidence: Annotated[
        tuple[HandoffCitation, ...],
        Field(max_length=MAX_HANDOFF_RECEIPT_EVIDENCE),
    ] = ()
    message: str | None = None

    @model_validator(mode="after")
    def validate_target_and_evidence(self) -> HandoffReceipt:
        if self.selection == "prepared":
            valid_target = self.selected_revision is None and self.prepared_digest is not None
        else:
            valid_target = self.selected_revision is not None and self.prepared_digest is None
        if not valid_target:
            raise ValueError("Handoff receipt must preserve its exact resolved target")  # noqa: TRY003
        if self.evidence_status == "available" and self.unavailable_evidence:
            raise ValueError("available Handoff receipt cannot contain unavailable evidence")  # noqa: TRY003
        if self.evidence_status == "unavailable" and not self.unavailable_evidence:
            raise ValueError("unavailable Handoff receipt must identify unavailable evidence")  # noqa: TRY003
        if self.status == "accepted" and self.evidence_status == "unavailable":
            raise ValueError("a Handoff with unavailable evidence cannot be accepted")  # noqa: TRY003
        return self


class WorkSourceReceipt(_WorkValue):
    """One typed Work record durably captured as Source evidence."""

    kind: WorkSourceKind
    source_ref: SourceRef
    position: Annotated[int, Field(ge=1)]
    content_digest: str


class PreparedWorkHandoff(_WorkValue):
    """One-step current-work capture and temporary Handoff preparation."""

    boundary: WorkSourceReceipt
    handoff: PreparedHandoff


class HandoffAcknowledgement(_WorkValue):
    """Resolved untrusted history plus the receiver's durable acknowledgement."""

    resolution: HandoffResolution
    receipt: WorkSourceReceipt


def content_digest(value: BaseModel) -> str:
    """Return a stable digest for one versioned record or Prepared Handoff."""

    payload = value.model_dump_json(by_alias=True, exclude_none=False)
    return f"sha256:{sha256(payload.encode()).hexdigest()}"


def _require_text(field: str, value: str) -> str:
    if not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be non-empty and trimmed")  # noqa: TRY003
    return value


def _require_text_items(field: str, values: tuple[str, ...]) -> tuple[str, ...]:
    for value in values:
        _require_text(field, value)
        if len(value) > MAX_WORK_TEXT_LENGTH:
            raise ValueError(f"{field} items must not exceed {MAX_WORK_TEXT_LENGTH} characters")  # noqa: TRY003
    if len(set(values)) != len(values):
        raise ValueError(f"{field} items must be unique")  # noqa: TRY003
    return values


__all__ = [
    "HANDOFF_BOUNDARY_SOURCE_KIND",
    "HANDOFF_RECEIPT_SOURCE_KIND",
    "TASK_OUTCOME_SOURCE_KIND",
    "WORK_CONTRACT_SOURCE_KIND",
    "AcknowledgeHandoff",
    "CreateWorkContract",
    "CurrentWorkHandoff",
    "HandoffAcknowledgement",
    "HandoffCurrentWork",
    "HandoffReceipt",
    "PreparedWorkHandoff",
    "RecordTaskOutcome",
    "TaskCheck",
    "TaskOutcome",
    "WorkClaim",
    "WorkContract",
    "WorkSourceKind",
    "WorkSourceReceipt",
    "content_digest",
]
