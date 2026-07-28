"""Minimal pinned Codex × SWE-bench Pro OFF/ON orchestration."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from powercontext_eval.artifacts import ArmState, ArtifactStore
from powercontext_eval.benchmarks.base import GoldResult, run_after_gold
from powercontext_eval.benchmarks.swebench_pro.adapter import SweBenchProInstance
from powercontext_eval.benchmarks.swebench_pro.evaluator import OfficialEvaluation, OfficialEvaluator
from powercontext_eval.benchmarks.swebench_pro.prediction import encode_predictions
from powercontext_eval.git_source import GitSource
from powercontext_eval.models import Arm, PowerContextRef
from powercontext_eval.paths import EvaluationPaths
from powercontext_eval.powercontext_sut import (
    ArmPaths,
    DockerSut,
    ProxyRelayConfig,
    SutConfig,
    SutOutcome,
    auth_secret_variants,
)
from powercontext_eval.process import ProcessRunner
from powercontext_eval.report import ArmReport, MetricSet, ReportBundle, render_report

INSTANCE_ID = "instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9"
TASK_IMAGE = "jefzda/sweap-images:flipt-io.flipt-flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9"
IMAGE_MANIFEST_DIGEST = "sha256:d2c9d5460c479cb257a0588a603021f4e83e31f2614146728336689854f52803"


class RunPhase(StrEnum):
    """Stable observable phases of a minimal evaluation run."""

    PREPARING = "preparing"
    VALIDATING_GOLD = "validating_gold"
    RUNNING_OFF = "running_off"
    RUNNING_ON = "running_on"
    OFFICIAL_EVALUATION = "official_evaluation"
    GENERATING_REPORT = "generating_report"


PhaseCallback = Callable[[RunPhase], None]


@dataclass(frozen=True)
class MinimalRunConfig:
    """All external inputs for one pinned experiment."""

    root: Path
    powercontext_source: Path
    powercontext_ref: str
    harness_root: Path
    harness_python: Path
    raw_sample_path: Path
    codex_binary: Path
    uv_binary: Path
    auth_json: Path
    proxy_url: str
    run_id: str | None = None


@dataclass(frozen=True)
class MinimalRunResult:
    """Paths and official outcomes returned by the one-command runner."""

    run_id: str
    report_path: Path
    off_resolved: bool
    on_resolved: bool


def run_minimal_swebench_pro(
    config: MinimalRunConfig,
    *,
    on_phase: PhaseCallback | None = None,
) -> MinimalRunResult:
    """Resolve PowerContext once, run Gold then OFF/ON, and render a report."""

    emit_phase = on_phase or (lambda phase: None)
    run_id = config.run_id or datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S")
    layout = EvaluationPaths(config.root.absolute(), run_id)
    if os.path.lexists(layout.run_artifacts) or os.path.lexists(config.root / "work" / run_id):
        raise ValueError(f"Run already exists: {run_id}")
    emit_phase(RunPhase.PREPARING)
    raw = _read_one_jsonl(config.raw_sample_path)
    instance = SweBenchProInstance.from_raw(raw, docker_manifest_digest=IMAGE_MANIFEST_DIGEST)
    if instance.instance_id != INSTANCE_ID:
        raise ValueError(f"The MVP supports only {INSTANCE_ID}")

    process = ProcessRunner()
    source = GitSource(cache_root=config.root / "cache" / "powercontext-git", runner=process)
    resolved = source.resolve(config.powercontext_source, PowerContextRef.parse(config.powercontext_ref))
    work_root = config.root / "work" / run_id
    materialized = source.materialize(resolved, work_root / "powercontext")

    run_store = ArtifactStore(layout.run_artifacts)
    run_store.create_json(
        "manifest.json",
        {
            "run_id": run_id,
            "instance_id": INSTANCE_ID,
            "powercontext_requested_ref": config.powercontext_ref,
            "powercontext_sha": resolved.sha,
            "task_image": TASK_IMAGE,
            "image_manifest_digest": IMAGE_MANIFEST_DIGEST,
        },
    )
    raw_copy = run_store.create_text(
        "instance.jsonl",
        json.dumps(raw, ensure_ascii=False, separators=(",", ":")) + "\n",
    )

    evaluator = OfficialEvaluator(process, python_executable=os.fspath(config.harness_python))
    gold_prediction = run_store.create_text(
        "gold/predictions.json",
        encode_predictions(INSTANCE_ID, instance.patch, "gold"),
    )
    emit_phase(RunPhase.VALIDATING_GOLD)
    gold = evaluator.evaluate(
        harness_root=config.harness_root,
        raw_sample_path=raw_copy,
        prediction_path=gold_prediction,
        output_dir=layout.run_artifacts / "gold" / "official",
        instance_id=INSTANCE_ID,
    )

    def arms() -> tuple[OfficialEvaluation, OfficialEvaluation, Mapping[Arm, SutOutcome], dict[Arm, int]]:
        secrets = auth_secret_variants(config.auth_json)
        arm_paths: dict[Arm, ArmPaths] = {}
        stores: dict[Arm, ArtifactStore] = {}
        for arm in (Arm.OFF, Arm.ON):
            arm_work = layout.arm_work(arm)
            runtime = arm_work / "runtime"
            arm_paths[arm] = ArmPaths(
                source=materialized,
                auth_source=config.auth_json,
                workspace=arm_work / "workspace",
                runtime=runtime,
                codex_home=runtime / "codex-home",
                pc_home=runtime / "pc-home",
                result_root=layout.arm_artifacts(arm),
            )
            stores[arm] = ArtifactStore(layout.arm_artifacts(arm), forbidden_values=secrets)
        outcomes = DockerSut(process).run_pair(
            SutConfig(
                run_id=run_id,
                task_image=TASK_IMAGE,
                codex_binary=config.codex_binary,
                uv_binary=config.uv_binary,
                source_checkout=materialized,
                plugin_checkout_sha=resolved.sha,
                proxy=ProxyRelayConfig(config.proxy_url),
            ),
            paths=arm_paths,
            prompts={Arm.OFF: instance.codex_prompt().encode(), Arm.ON: instance.codex_prompt().encode()},
            stores=stores,
            before_arm=lambda arm: emit_phase(RunPhase.RUNNING_OFF if arm is Arm.OFF else RunPhase.RUNNING_ON),
        )
        official: dict[Arm, OfficialEvaluation] = {}
        patch_sizes: dict[Arm, int] = {}
        emit_phase(RunPhase.OFFICIAL_EVALUATION)
        for arm in (Arm.OFF, Arm.ON):
            patch = process.run(
                ("git", "diff", "--binary", "--full-index", instance.base_commit, "--"),
                cwd=arm_paths[arm].workspace,
                timeout=120,
            ).stdout
            patch_sizes[arm] = len(patch.encode())
            prediction = stores[arm].create_text(
                "prediction.json",
                encode_predictions(INSTANCE_ID, patch, "codex-0.145.0"),
            )
            official[arm] = evaluator.evaluate(
                harness_root=config.harness_root,
                raw_sample_path=raw_copy,
                prediction_path=prediction,
                output_dir=layout.arm_artifacts(arm) / "official",
                instance_id=INSTANCE_ID,
            )
        return official[Arm.OFF], official[Arm.ON], outcomes, patch_sizes

    off_eval, on_eval, outcomes, patch_sizes = run_after_gold(
        GoldResult(INSTANCE_ID, gold.resolved),
        arms,
    )
    off_outcome = outcomes[Arm.OFF]
    on_outcome = outcomes[Arm.ON]
    emit_phase(RunPhase.GENERATING_REPORT)
    report = ReportBundle(
        title="PowerContext Codex SWE-bench Pro comparison",
        revisions={
            "dataset": "7ab5114912baf22bb098818e604c02fe7ad2c11f",
            "harness": "ca10a60a5fcae51e6948ffe1485d4153d421e6c5",
            "powercontext": resolved.sha,
        },
        configuration={
            "codex": "0.145.0",
            "instance": INSTANCE_ID,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
        },
        off=_arm_report(Arm.OFF, off_eval, off_outcome, patch_sizes[Arm.OFF]),
        on=_arm_report(Arm.ON, on_eval, on_outcome, patch_sizes[Arm.ON]),
    )
    rendered = render_report(report)
    if render_report(report) != rendered:
        raise RuntimeError("Report rendering is not deterministic")
    report_path = run_store.create_text("report.md", rendered)
    run_store.create_json("report.json", report.model_dump(mode="json"))
    return MinimalRunResult(run_id, report_path, off_eval.resolved, on_eval.resolved)


def _read_one_jsonl(path: Path) -> dict[str, object]:
    lines = path.read_text().splitlines()
    if len(lines) != 1:
        raise ValueError("Pinned raw sample must contain exactly one JSONL record")
    value = json.loads(lines[0])
    if not isinstance(value, dict):
        raise TypeError("Pinned raw sample must be a JSON object")
    return value


def _arm_report(arm: Arm, evaluation: OfficialEvaluation, outcome: SutOutcome, patch_bytes: int) -> ArmReport:
    usage = outcome.codex.usage
    return ArmReport(
        arm=arm.value,
        state=ArmState.TREATMENT_VALIDATED,
        resolved=evaluation.resolved,
        passed=evaluation.resolved,
        treatment_valid=True,
        metrics=MetricSet(
            patch_bytes=patch_bytes,
            input_tokens=None if usage is None else usage.get("input_tokens"),
            output_tokens=None if usage is None else usage.get("output_tokens"),
        ),
    )
