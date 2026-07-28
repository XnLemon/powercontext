"""Pure deterministic Markdown report rendering."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticSerializationError

from powercontext_eval.artifacts import ArmState


class MetricSet(BaseModel):
    """Comparable measurements captured from one arm."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    patch_bytes: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    elapsed_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ArmReport(BaseModel):
    """Audited report input for one fixed treatment arm."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    arm: Literal["off", "on"]
    state: ArmState
    resolved: bool
    passed: bool | None
    treatment_valid: bool
    metrics: MetricSet = Field(default_factory=MetricSet)
    failure_status: str | None = None
    invalid_reason: str | None = None


class ReportBundle(BaseModel):
    """Complete, side-effect-free input to report rendering."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: str
    revisions: Mapping[str, str]
    configuration: Mapping[str, str]
    off: ArmReport
    on: ArmReport

    @field_validator("revisions", "configuration")
    @classmethod
    def reject_sensitive_mapping_keys(cls, values: Mapping[str, str]) -> Mapping[str, str]:
        """Keep credential-shaped fields and environment dumps out of retained reports."""

        forbidden_segments = {
            "api_key",
            "authorization",
            "cookie",
            "credential",
            "credentials",
            "env",
            "environment",
            "passwd",
            "password",
            "secret",
            "token",
        }
        for key in values:
            normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
            segments = set(normalized.split("_"))
            if normalized in forbidden_segments or segments & forbidden_segments:
                raise ValueError("Report mapping contains a forbidden field name")
        return values


class InvalidReportBundle(ValueError):
    """A report bundle failed safe boundary revalidation."""


def _cell(value: object | None) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "N/A"
        rendered = format(value, ".12g")
    else:
        rendered = str(value)
    return " ".join(rendered.split()).replace("|", "\\|")


def _status(value: bool | None, true: str, false: str) -> str:
    if value is None:
        return "N/A"
    return true if value else false


def _mapping_table(title: str, values: Mapping[str, str]) -> list[str]:
    lines = [f"## {title}", "", "| Key | Value |", "| --- | --- |"]
    lines.extend(f"| {_cell(key)} | {_cell(values[key])} |" for key in sorted(values))
    if not values:
        lines.append("| N/A | N/A |")
    lines.append("")
    return lines


def _arm_section(label: str, arm: ArmReport) -> list[str]:
    metrics = arm.metrics
    details = arm.failure_status or arm.invalid_reason
    return [
        f"## PowerContext {label}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Resolution status | {_status(arm.resolved, 'RESOLVED', 'UNRESOLVED')} |",
        f"| Lifecycle state | {_cell(arm.state.value)} |",
        f"| Pass status | {_status(arm.passed, 'PASS', 'FAIL')} |",
        f"| Treatment validity | {_status(arm.treatment_valid, 'VALID', 'INVALID')} |",
        f"| Patch bytes | {_cell(metrics.patch_bytes)} |",
        f"| Input tokens | {_cell(metrics.input_tokens)} |",
        f"| Output tokens | {_cell(metrics.output_tokens)} |",
        f"| Elapsed seconds | {_cell(metrics.elapsed_seconds)} |",
        f"| Failure or invalid reason | {_cell(details)} |",
        "",
    ]


def _comparison(bundle: ReportBundle) -> list[str]:
    lines = ["## Comparison", ""]
    comparable_states = {ArmState.TREATMENT_VALIDATED, ArmState.REPORTED}
    if not (
        bundle.off.state in comparable_states
        and bundle.on.state in comparable_states
        and bundle.off.treatment_valid
        and bundle.on.treatment_valid
    ):
        return lines + ["Comparison unavailable: both arms must have validated treatment.", ""]

    off_metrics = bundle.off.metrics
    on_metrics = bundle.on.metrics
    comparable = (
        ("Patch bytes delta", off_metrics.patch_bytes, on_metrics.patch_bytes),
        ("Input tokens delta", off_metrics.input_tokens, on_metrics.input_tokens),
        ("Output tokens delta", off_metrics.output_tokens, on_metrics.output_tokens),
        ("Elapsed seconds delta", off_metrics.elapsed_seconds, on_metrics.elapsed_seconds),
    )
    available = [(name, off, on) for name, off, on in comparable if off is not None and on is not None]
    if bundle.off.passed is None or bundle.on.passed is None or not available:
        return lines + ["Comparison unavailable: comparable metrics are missing.", ""]

    lines.extend(["| Metric | ON minus OFF |", "| --- | --- |"])
    pass_delta = int(bundle.on.passed) - int(bundle.off.passed)
    lines.append(f"| Pass delta | {pass_delta:+d} |")
    for name, off_value, on_value in available:
        assert off_value is not None and on_value is not None
        delta = on_value - off_value
        rendered = f"{delta:+.12g}" if isinstance(delta, float) else f"{delta:+d}"
        lines.append(f"| {name} | {rendered} |")
    lines.append("")
    return lines


def _validated_bundle(bundle: ReportBundle) -> ReportBundle:
    """Revalidate copied or mutated model contents without reflecting rejected values."""

    try:
        if type(bundle) is not ReportBundle:
            raise TypeError
        expected_bundle_fields = set(ReportBundle.model_fields)
        if set(bundle.__dict__) != expected_bundle_fields:
            raise ValueError
        for model, model_type in (
            (bundle.off, ArmReport),
            (bundle.on, ArmReport),
        ):
            if type(model) is not model_type or set(model.__dict__) != set(model_type.model_fields):
                raise ValueError
            if type(model.metrics) is not MetricSet or set(model.metrics.__dict__) != set(MetricSet.model_fields):
                raise ValueError
        serialized = bundle.model_dump(mode="python", round_trip=True, warnings="none")
        return ReportBundle.model_validate(serialized, strict=True)
    except (AttributeError, PydanticSerializationError, TypeError, ValueError):
        raise InvalidReportBundle("Report bundle failed strict validation") from None


def render_report(bundle: ReportBundle) -> str:
    """Render only the supplied validated bundle, with no external reads."""

    bundle = _validated_bundle(bundle)
    if bundle.off.arm != "off" or bundle.on.arm != "on":
        raise ValueError("Report arms must be supplied in OFF then ON roles")
    lines = [f"# {_cell(bundle.title)}", ""]
    lines.extend(_mapping_table("Resolved revisions", bundle.revisions))
    lines.extend(_mapping_table("Configuration", bundle.configuration))
    lines.extend(_arm_section("OFF", bundle.off))
    lines.extend(_arm_section("ON", bundle.on))
    lines.extend(_comparison(bundle))
    return "\n".join(lines)
