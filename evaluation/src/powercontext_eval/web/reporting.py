"""Validated, bounded projection of retained evaluation reports."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from powercontext_eval.artifacts import ArmState
from powercontext_eval.report import ArmReport, MetricSet, ReportBundle
from powercontext_eval.web.models import (
    ArmResponse,
    ComparisonResponse,
    EvidenceResponse,
    MetricComparison,
    ReportResponse,
    TreatmentEvidence,
)

_REPORT_JSON_LIMIT = 1024 * 1024
_REPORT_MARKDOWN_LIMIT = 4 * 1024 * 1024
_TREATMENT_LIMIT = 64 * 1024
_PLUGIN_ID = "powercontext@powercontext"
_COMPARABLE_STATES = {ArmState.TREATMENT_VALIDATED, ArmState.REPORTED}


class ReportingError(Exception):
    """Safe base exception for retained report access."""


class UnsafeReportPath(ReportingError):
    """The requested run is not a safe child of the configured run root."""

    def __init__(self) -> None:
        super().__init__("Evaluation run path is unsafe")


class InvalidReportArtifact(ReportingError):
    """One or more retained report artifacts failed validation."""

    def __init__(self) -> None:
        super().__init__("Evaluation report artifacts are invalid")


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_run(run_dir: Path, run_root: Path | None) -> tuple[int, str]:
    requested = run_dir.absolute()
    try:
        root = (requested.parent if run_root is None else run_root).resolve(strict=True)
        metadata = requested.lstat()
        if (
            requested.parent.resolve(strict=True) != root
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
        ):
            raise UnsafeReportPath
        descriptor = os.open(requested, _directory_flags())
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            os.close(descriptor)
            raise UnsafeReportPath
    except UnsafeReportPath:
        raise
    except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError):
        raise UnsafeReportPath from None
    return descriptor, requested.name


def _open_relative_directory(root_fd: int, components: tuple[str, ...]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for component in components:
            next_descriptor = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_bounded(root_fd: int, relative: tuple[str, ...], limit: int) -> tuple[bytes, os.stat_result]:
    parent_fd = -1
    descriptor = -1
    try:
        parent_fd = _open_relative_directory(root_fd, relative[:-1])
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(relative[-1], flags, dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise InvalidReportArtifact
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise InvalidReportArtifact
        return data, metadata
    except InvalidReportArtifact:
        raise
    except (FileNotFoundError, NotADirectoryError, OSError):
        raise InvalidReportArtifact from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)


def _load_bundle(run_fd: int) -> tuple[ReportBundle, os.stat_result]:
    raw, metadata = _read_bounded(run_fd, ("report.json",), _REPORT_JSON_LIMIT)
    try:
        bundle = ReportBundle.model_validate_json(raw, strict=True)
    except (ValidationError, ValueError, UnicodeDecodeError):
        raise InvalidReportArtifact from None
    if bundle.off.arm != "off" or bundle.on.arm != "on":
        raise InvalidReportArtifact
    return bundle, metadata


def _load_evidence(run_fd: int, arm: str) -> TreatmentEvidence:
    raw, _ = _read_bounded(
        run_fd,
        ("arms", arm, "powercontext", "treatment.json"),
        _TREATMENT_LIMIT,
    )
    try:
        return TreatmentEvidence.model_validate_json(raw, strict=True)
    except (ValidationError, ValueError, UnicodeDecodeError):
        raise InvalidReportArtifact from None


def _validate_evidence(
    bundle: ReportBundle,
    run_id: str,
    off: TreatmentEvidence,
    on: TreatmentEvidence,
) -> None:
    expected_sha = bundle.revisions.get("powercontext")
    configured_plugin_id = bundle.configuration.get("plugin_id", _PLUGIN_ID)
    configured_plugin_version = bundle.configuration.get("plugin_version", off.plugin_version)
    common = (
        expected_sha is not None
        and off.plugin_checkout_sha == expected_sha
        and on.plugin_checkout_sha == expected_sha
        and off.plugin_id == configured_plugin_id == on.plugin_id == _PLUGIN_ID
        and bool(off.plugin_version)
        and off.plugin_version == configured_plugin_version == on.plugin_version
        and off.plugin_installed
        and on.plugin_installed
        and off.server_ready
        and on.server_ready
        and off.scope_id == f"eval:{run_id}:off"
        and on.scope_id == f"eval:{run_id}:on"
    )
    activity = off.prompt_sources == 0 and off.mcp_requests == 0 and on.prompt_sources > 0 and on.mcp_requests > 0
    if not common or not activity:
        raise InvalidReportArtifact


def _arm_response(arm: Literal["off", "on"], report: ArmReport) -> ArmResponse:
    return ArmResponse(
        arm=arm,
        resolution="resolved" if report.resolved else "unresolved",
        input_tokens=report.metrics.input_tokens,
        output_tokens=report.metrics.output_tokens,
        elapsed_seconds=report.metrics.elapsed_seconds,
        patch_bytes=report.metrics.patch_bytes,
    )


def _comparison(off: float | None, on: float | None) -> MetricComparison | None:
    if off is None or on is None:
        return None
    delta = on - off
    percent = None if off == 0 else delta / off * 100
    return MetricComparison(off=off, on=on, delta=delta, percent=percent)


def _comparisons(off: MetricSet, on: MetricSet) -> ComparisonResponse:
    return ComparisonResponse(
        input_tokens=_comparison(off.input_tokens, on.input_tokens),
        output_tokens=_comparison(off.output_tokens, on.output_tokens),
        elapsed_seconds=_comparison(off.elapsed_seconds, on.elapsed_seconds),
        patch_bytes=_comparison(off.patch_bytes, on.patch_bytes),
    )


def _acceptance_valid(bundle: ReportBundle) -> bool:
    lifecycle_is_comparable = bundle.off.state == bundle.on.state and bundle.off.state in _COMPARABLE_STATES
    official_outcomes_are_coherent = (
        bundle.off.passed is True and bundle.on.passed is True and bundle.off.resolved and bundle.on.resolved
    )
    return (
        bundle.off.treatment_valid
        and bundle.on.treatment_valid
        and lifecycle_is_comparable
        and official_outcomes_are_coherent
    )


def load_report(run_dir: Path, run_root: Path | None = None) -> ReportResponse:
    """Load a report only after validating its retained bundle and treatment evidence."""

    run_fd, run_id = _open_run(run_dir, run_root)
    try:
        bundle, report_metadata = _load_bundle(run_fd)
        off_evidence = _load_evidence(run_fd, "off")
        on_evidence = _load_evidence(run_fd, "on")
        _validate_evidence(bundle, run_id, off_evidence, on_evidence)
        return ReportResponse(
            task_id=run_id,
            acceptance_valid=_acceptance_valid(bundle),
            off=_arm_response("off", bundle.off),
            on=_arm_response("on", bundle.on),
            comparison=_comparisons(bundle.off.metrics, bundle.on.metrics),
            evidence=EvidenceResponse(off=off_evidence, on=on_evidence),
            revisions=bundle.revisions,
            configuration=bundle.configuration,
            generated_at=datetime.fromtimestamp(report_metadata.st_mtime, tz=UTC),
        )
    except (InvalidReportArtifact, UnsafeReportPath):
        raise
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise InvalidReportArtifact from None
    finally:
        os.close(run_fd)


def load_raw_report(run_dir: Path, run_root: Path | None = None) -> str:
    """Return bounded UTF-8 Markdown as literal text for a ``text/plain`` response."""

    run_fd, _ = _open_run(run_dir, run_root)
    try:
        raw, _ = _read_bounded(run_fd, ("report.md",), _REPORT_MARKDOWN_LIMIT)
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise InvalidReportArtifact from None
    finally:
        os.close(run_fd)
