from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    HandoffContent,
    HandoffSourceCitation,
    HandoffStatement,
    InvalidRuntimeRequestError,
    PreparedHandoff,
    open_builtin_runtime,
)
from powercontext.builtin.work import (
    AcknowledgeHandoff,
    CreateWorkContract,
    WorkClaim,
    WorkContract,
)
from powercontext.sources import SourceRef


def test_verified_work_claims_require_exact_evidence() -> None:
    with pytest.raises(ValidationError, match="verified Work claims require exact evidence"):
        WorkClaim(text="Tests pass.", basis="verified")

    with pytest.raises(ValidationError, match="declared Work claims cannot present evidence as verified"):
        WorkClaim(
            text="Tests pass.",
            basis="declared",
            evidence=(
                HandoffSourceCitation(
                    source_ref=SourceRef(source_type="content", source_id="test-output"),
                ),
            ),
        )

    with pytest.raises(ValidationError, match="at most 31 items"):
        WorkClaim(
            text="The handoff reserves one citation for its captured boundary.",
            basis="verified",
            evidence=tuple(
                HandoffSourceCitation(
                    source_ref=SourceRef(source_type="content", source_id=f"evidence-{index}"),
                )
                for index in range(32)
            ),
        )


def test_acknowledgement_cannot_accept_unavailable_handoff_evidence() -> None:
    async def scenario() -> None:
        missing = HandoffSourceCitation(
            source_ref=SourceRef(source_type="content", source_id="missing-output"),
        )
        prepared = PreparedHandoff(
            scope_id="project",
            base=None,
            content=HandoffContent(
                objective="Continue a partially verified change.",
                state=(HandoffStatement(text="The change was reported as implemented.", citations=(missing,)),),
                disposition="continuable",
            ),
        )
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            work = runtime.work.for_scope("project")

            with pytest.raises(InvalidRuntimeRequestError, match="handoff-evidence-unavailable"):
                await work.acknowledge(
                    AcknowledgeHandoff(
                        source_id="receipt-accepted",
                        receiver="receiver-agent",
                        status="accepted",
                        selection="prepared",
                        prepared=prepared,
                    )
                )

            clarification = await work.acknowledge(
                AcknowledgeHandoff(
                    source_id="receipt-clarification",
                    receiver="receiver-agent",
                    status="needs_clarification",
                    selection="prepared",
                    prepared=prepared,
                    message="The cited implementation output is unavailable.",
                )
            )

        assert clarification.resolution.evidence_checks[0].status == "unavailable"
        assert clarification.receipt.kind == "handoff-receipt"
        assert clarification.receipt.position == 1

    asyncio.run(scenario())


def test_work_contract_rejects_a_verified_cross_record_claim_when_evidence_is_missing() -> None:
    async def scenario() -> None:
        contract = WorkContract(
            objective="Use only evidence-backed facts.",
            facts=(
                WorkClaim(
                    text="A regression test passed.",
                    basis="verified",
                    evidence=(
                        HandoffSourceCitation(
                            source_ref=SourceRef(source_type="content", source_id="missing-test-output"),
                        ),
                    ),
                ),
            ),
            in_scope=("Record a grounded delegation baseline.",),
            completion_criteria=("Reject unavailable verified evidence.",),
        )
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            with pytest.raises(LookupError):
                await runtime.work.for_scope("project").create_contract(
                    CreateWorkContract(source_id="contract-1", contract=contract)
                )

    asyncio.run(scenario())
