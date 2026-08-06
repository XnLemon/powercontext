from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from powercontext_eval.artifacts import ArmState
from powercontext_eval.benchmarks.swebench_pro.gold_overrides import (
    SOURCE559_DATASET_PATCH_SHA256,
    SOURCE559_INSTANCE_ID,
    SOURCE559_REFERENCE_PATCH,
    SOURCE559_REFERENCE_PATCH_SHA256,
    GoldValidationOverrideError,
    select_gold_validation,
)
from powercontext_eval.report import ArmReport, GoldValidationAudit, ReportBundle


def _source559_dataset_patch() -> str:
    return "source559-original-patch"


def _source559_audit(status: str = "passed") -> GoldValidationAudit:
    return GoldValidationAudit(
        instance_id=SOURCE559_INSTANCE_ID,
        mode="verified_override",
        dataset_patch_sha256=SOURCE559_DATASET_PATCH_SHA256,
        validation_patch_sha256=SOURCE559_REFERENCE_PATCH_SHA256,
        dataset_patch_status="known_failed",
        reference_validation_status="passed",
        attempt_gold_validation_status=status,
        source_dataset="livesweagent/claude-sonnet-4-5_swebench_pro_traj",
        source_revision="e9c3cf3611956d75ad8a78b9cce5b4a524828e22",
        source_file_oid="7d910a550fc80f16647b795e2ab23fa032ac91fa",
        source_kind="verified_reference_submission",
    )


def _dataset_audit(status: str = "pending") -> GoldValidationAudit:
    audit = select_gold_validation("instance_other", "ordinary patch").audit
    audit["attempt_gold_validation_status"] = status
    return GoldValidationAudit(**audit)


def test_reference_patch_is_static_and_hash_pinned() -> None:
    assert len(SOURCE559_REFERENCE_PATCH.encode()) == 3303
    assert hashlib.sha256(SOURCE559_REFERENCE_PATCH.encode()).hexdigest() == SOURCE559_REFERENCE_PATCH_SHA256


def test_exact_instance_selects_override_and_audits_pending_before_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    original = _source559_dataset_patch()
    import powercontext_eval.benchmarks.swebench_pro.gold_overrides as overrides

    monkeypatch.setattr(overrides, "SOURCE559_DATASET_PATCH_SHA256", hashlib.sha256(original.encode()).hexdigest())
    selection = select_gold_validation(SOURCE559_INSTANCE_ID, original)
    assert selection.mode == "verified_override"
    assert selection.validation_patch == SOURCE559_REFERENCE_PATCH
    assert selection.audit["attempt_gold_validation_status"] == "pending"
    assert selection.audit["dataset_patch_sha256"] == hashlib.sha256(original.encode()).hexdigest()


def test_hash_drift_fails_closed() -> None:
    with pytest.raises(GoldValidationOverrideError):
        select_gold_validation(SOURCE559_INSTANCE_ID, "changed dataset patch")


def test_other_instances_keep_original_patch_and_have_no_reference_provenance() -> None:
    selection = select_gold_validation("instance_other", "ordinary patch")
    assert selection.validation_patch == "ordinary patch"
    assert selection.mode == "dataset_patch"
    assert selection.audit["source_dataset"] is None
    assert selection.audit["attempt_gold_validation_status"] == "pending"


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_source559_report_requires_successful_verified_audit(status: str) -> None:
    base = {
        "title": "test",
        "revisions": {"powercontext": "a" * 40},
        "configuration": {"instance": SOURCE559_INSTANCE_ID},
        "off": ArmReport(
            arm="off", state=ArmState.TREATMENT_VALIDATED, resolved=True, passed=True, treatment_valid=True
        ),
        "on": ArmReport(arm="on", state=ArmState.TREATMENT_VALIDATED, resolved=True, passed=True, treatment_valid=True),
    }
    with pytest.raises(ValidationError):
        ReportBundle(**base)
    with pytest.raises(ValidationError):
        ReportBundle(**base, gold_validation=_source559_audit(status=status))
    report = ReportBundle(**base, gold_validation=_source559_audit())
    assert report.gold_validation is not None


def test_source559_report_rejects_tampered_audit() -> None:
    base = {
        "title": "test",
        "revisions": {"powercontext": "a" * 40},
        "configuration": {"instance": SOURCE559_INSTANCE_ID},
        "off": ArmReport(
            arm="off", state=ArmState.TREATMENT_VALIDATED, resolved=True, passed=True, treatment_valid=True
        ),
        "on": ArmReport(arm="on", state=ArmState.TREATMENT_VALIDATED, resolved=True, passed=True, treatment_valid=True),
    }
    data = _source559_audit().model_dump()
    data["source_file_oid"] = "0" * 40
    with pytest.raises(ValidationError):
        ReportBundle(**base, gold_validation=data)


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_any_report_rejects_unsuccessful_gold_audit(status: str) -> None:
    ordinary = ReportBundle(
        title="test",
        revisions={"powercontext": "a" * 40},
        configuration={"instance": "instance_other"},
        off=ArmReport(arm="off", state=ArmState.TREATMENT_VALIDATED, resolved=True, passed=True, treatment_valid=True),
        on=ArmReport(arm="on", state=ArmState.TREATMENT_VALIDATED, resolved=True, passed=True, treatment_valid=True),
    )
    with pytest.raises(ValidationError):
        ReportBundle.model_validate(
            ordinary.model_dump(mode="json") | {"gold_validation": _dataset_audit(status).model_dump(mode="json")},
            strict=True,
        )


def test_non_source559_cannot_claim_verified_override_and_old_reports_still_parse() -> None:
    ordinary = ReportBundle(
        title="test",
        revisions={"powercontext": "a" * 40},
        configuration={"instance": "instance_other"},
        off=ArmReport(arm="off", state=ArmState.TREATMENT_VALIDATED, resolved=True, passed=True, treatment_valid=True),
        on=ArmReport(arm="on", state=ArmState.TREATMENT_VALIDATED, resolved=True, passed=True, treatment_valid=True),
    )
    old_payload = ordinary.model_dump(mode="json")
    old_payload.pop("gold_validation", None)
    assert ReportBundle.model_validate(old_payload, strict=True).gold_validation is None
    with pytest.raises(ValidationError):
        ReportBundle.model_validate(
            ordinary.model_dump(mode="json") | {"gold_validation": _source559_audit().model_dump()}, strict=True
        )
