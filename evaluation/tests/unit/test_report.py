from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from powercontext_eval.artifacts import ArmState
from powercontext_eval.report import ArmReport, MetricSet, ReportBundle, render_report


def _valid_bundle() -> ReportBundle:
    return ReportBundle(
        title="SWE-bench Pro evaluation",
        revisions={"powercontext": "b" * 40, "harness": "a" * 40},
        configuration={"model": "gpt-5.6-sol", "effort": "medium"},
        off=ArmReport(
            arm="off",
            state=ArmState.TREATMENT_VALIDATED,
            resolved=True,
            passed=False,
            treatment_valid=True,
            metrics=MetricSet(patch_bytes=100, input_tokens=200, output_tokens=30, elapsed_seconds=12.25),
        ),
        on=ArmReport(
            arm="on",
            state=ArmState.TREATMENT_VALIDATED,
            resolved=True,
            passed=True,
            treatment_valid=True,
            metrics=MetricSet(patch_bytes=90, input_tokens=180, output_tokens=25, elapsed_seconds=11.5),
        ),
    )


def test_report_is_deterministic_and_orders_off_before_on() -> None:
    bundle = _valid_bundle()
    first = render_report(bundle)
    second = render_report(bundle)
    assert first == second
    assert first.index("PowerContext OFF") < first.index("PowerContext ON")
    assert "Pass status | FAIL" in first
    assert "Pass status | PASS" in first
    assert "## Comparison" in first
    assert "Pass delta | +1" in first


def test_mapping_insertion_order_does_not_change_report() -> None:
    first = _valid_bundle()
    second = first.model_copy(
        update={
            "revisions": dict(reversed(list(first.revisions.items()))),
            "configuration": dict(reversed(list(first.configuration.items()))),
        }
    )
    assert render_report(first) == render_report(second)


def test_missing_metrics_render_na_and_do_not_invent_zero() -> None:
    bundle = _valid_bundle().model_copy(
        update={
            "off": _valid_bundle().off.model_copy(update={"metrics": MetricSet()}),
            "on": _valid_bundle().on.model_copy(update={"metrics": MetricSet()}),
        }
    )
    report = render_report(bundle)
    assert "Patch bytes | N/A" in report
    assert "Input tokens | N/A" in report
    assert "Elapsed seconds | N/A" in report
    assert "Comparison unavailable" in report
    assert "comparable metrics are missing" in report


def test_invalid_treatment_is_not_scored_as_failure() -> None:
    bundle = _valid_bundle().model_copy(
        update={
            "on": ArmReport(
                arm="on",
                state=ArmState.INVALID_TREATMENT,
                resolved=True,
                passed=None,
                treatment_valid=False,
                invalid_reason="PowerContext prompt hook evidence is missing",
            )
        }
    )
    report = render_report(bundle)
    assert "Treatment validity | INVALID" in report
    assert "Pass status | N/A" in report
    assert "Comparison unavailable" in report
    assert "both arms must have validated treatment" in report
    assert "PowerContext prompt hook evidence is missing" in report


def test_infrastructure_failure_and_table_text_are_normalized() -> None:
    bundle = _valid_bundle().model_copy(
        update={
            "off": ArmReport(
                arm="off",
                state=ArmState.INFRASTRUCTURE_ERROR,
                resolved=False,
                passed=None,
                treatment_valid=False,
                failure_status="proxy |\n unavailable",
            )
        }
    )
    report = render_report(bundle)
    assert "proxy \\| unavailable" in report
    assert "Resolution status | UNRESOLVED" in report
    assert "Comparison unavailable" in report


def test_renderer_rejects_unknown_or_sensitive_fields() -> None:
    data = _valid_bundle().model_dump()
    data["environment"] = {"TOKEN": "secret"}
    with pytest.raises(ValidationError):
        ReportBundle.model_validate(data)

    arm = _valid_bundle().off.model_dump()
    arm["api_key"] = "secret"
    with pytest.raises(ValidationError):
        ArmReport.model_validate(arm)

    data = _valid_bundle().model_dump()
    data["configuration"]["api_token"] = "secret"
    with pytest.raises(ValidationError):
        ReportBundle.model_validate(data)


def test_renderer_has_no_process_network_time_or_filesystem_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("renderer attempted an external side effect")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("time.time", forbidden)
    monkeypatch.chdir(tmp_path)
    before = list(tmp_path.iterdir())
    render_report(_valid_bundle())
    assert list(tmp_path.iterdir()) == before


def test_float_format_and_trailing_newline_are_stable() -> None:
    report = render_report(_valid_bundle())
    assert "Elapsed seconds | 12.25" in report
    assert report.endswith("\n")
