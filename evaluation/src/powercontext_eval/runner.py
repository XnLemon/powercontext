"""Codex × SWE-bench Pro OFF/ON orchestration for one pinned instance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from powercontext_eval.artifacts import ArmState, ArtifactStore
from powercontext_eval.benchmarks.base import GoldResult, run_after_gold
from powercontext_eval.benchmarks.swebench_pro.adapter import (
    DATASET_REVISION,
    HARNESS_COMMIT,
    SweBenchProInstance,
)
from powercontext_eval.benchmarks.swebench_pro.evaluator import OfficialEvaluation, OfficialEvaluator
from powercontext_eval.benchmarks.swebench_pro.prediction import encode_predictions
from powercontext_eval.codex import DEFAULT_CODEX_MODEL, DEFAULT_REASONING_EFFORT, is_safe_codex_model
from powercontext_eval.context_trace import write_context_trace
from powercontext_eval.errors import CommandFailed
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
    loopback_proxy_environment,
)
from powercontext_eval.process import ProcessRunner
from powercontext_eval.report import ArmReport, MetricSet, ReportBundle, TestGroupReport, render_report
from powercontext_eval.tokensflow import (
    TokensFlowInfrastructureError,
    UnsafeTokensFlowConfiguration,
    snapshot_tokensflow_home,
    tokensflow_secret_variants,
)

# Compatibility identifier for the legacy single-task web contract. The generic runner never consults it.
INSTANCE_ID = "instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9"
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE_REMOVAL_ATTEMPTS = 5
_IMAGE_REMOVAL_RETRY_SECONDS = 0.25
_TRANSIENT_IMAGE_REMOVAL_ERRORS = (
    "is using its referenced image",
    "is being used by stopped container",
)
_OPENLIBRARY_DYNAMIC_YEAR_PREFIX = (
    "openlibrary/catalog/add_book/tests/test_add_book.py::TestNormalizeImportRecord::"
    "test_future_publication_dates_are_deleted"
)


class RunPhase(StrEnum):
    """Stable observable phases of one evaluation run."""

    PREPARING = "preparing"
    VALIDATING_GOLD = "validating_gold"
    RUNNING_OFF = "running_off"
    RUNNING_ON = "running_on"
    OFFICIAL_EVALUATION = "official_evaluation"
    GENERATING_REPORT = "generating_report"


PhaseCallback = Callable[[RunPhase], None]


@dataclass(frozen=True)
class RunConfig:
    """All external inputs shared by one pinned instance pair."""

    root: Path
    powercontext_source: Path
    powercontext_ref: str
    harness_root: Path
    harness_python: Path
    codex_binary: Path
    tokensflow_binary: Path
    tokensflow_user_home: Path
    tokensflow_egress_network: str
    uv_binary: Path
    registry_binary: Path
    auth_json: Path
    proxy_url: str
    run_id: str
    model: str = DEFAULT_CODEX_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT

    def __post_init__(self) -> None:
        if not is_safe_codex_model(self.model):
            raise ValueError("Codex model is unsafe")
        if self.reasoning_effort != DEFAULT_REASONING_EFFORT:
            raise ValueError("Unsupported Codex reasoning effort")


@dataclass(frozen=True)
class RunResult:
    """Paths and official outcomes returned by the per-instance runner."""

    run_id: str
    report_path: Path
    off_resolved: bool
    on_resolved: bool


# Public compatibility name retained while the web task schema migrates from a single task to batches.
MinimalRunResult = RunResult


def run_swebench_pro_instance(
    config: RunConfig,
    *,
    instance: SweBenchProInstance,
    on_phase: PhaseCallback | None = None,
) -> RunResult:
    """Run one instance and release a task image imported solely for this run."""

    process = ProcessRunner()
    image_cwd = config.root.absolute().parent
    image_was_present = _inspect_task_image(process, instance.task_image, cwd=image_cwd) is not None
    try:
        return _run_swebench_pro_instance(
            config,
            instance=instance,
            on_phase=on_phase,
            process=process,
        )
    finally:
        if not image_was_present and _inspect_task_image(process, instance.task_image, cwd=image_cwd) is not None:
            _remove_imported_task_image(process, instance.task_image, cwd=image_cwd)


def _remove_imported_task_image(process: ProcessRunner, task_image: str, *, cwd: Path) -> None:
    command = ("docker", "image", "rm", task_image)
    for attempt in range(_IMAGE_REMOVAL_ATTEMPTS):
        try:
            process.run(command, cwd=cwd, timeout=600)
            return
        except CommandFailed as error:
            retryable = any(marker in error.result.stderr for marker in _TRANSIENT_IMAGE_REMOVAL_ERRORS)
            if not retryable or attempt == _IMAGE_REMOVAL_ATTEMPTS - 1:
                raise
            time.sleep(_IMAGE_REMOVAL_RETRY_SECONDS)


def _run_swebench_pro_instance(
    config: RunConfig,
    *,
    instance: SweBenchProInstance,
    on_phase: PhaseCallback | None,
    process: ProcessRunner,
) -> RunResult:
    """Run Gold then OFF/ON for exactly the supplied catalog instance."""

    emit_phase = on_phase if on_phase is not None else (lambda phase: None)
    run_id = config.run_id
    layout = EvaluationPaths(config.root.absolute(), run_id)
    if os.path.lexists(layout.run_artifacts) or os.path.lexists(config.root / "work" / run_id):
        raise ValueError(f"Run already exists: {run_id}")
    emit_phase(RunPhase.PREPARING)

    source = GitSource(cache_root=config.root / "cache" / "powercontext-git", runner=process)
    resolved = source.resolve(config.powercontext_source, PowerContextRef.parse(config.powercontext_ref))
    work_root = config.root / "work" / run_id
    materialized = source.materialize(resolved, work_root / "powercontext")

    run_store = ArtifactStore(layout.run_artifacts)
    task_image_id = _resolve_task_image(
        process,
        instance.task_image,
        cwd=layout.run_artifacts,
        registry_binary=config.registry_binary,
        proxy_url=config.proxy_url,
    )
    run_store.create_json(
        "manifest.json",
        {
            "run_id": run_id,
            "instance_id": instance.instance_id,
            "powercontext_requested_ref": config.powercontext_ref,
            "powercontext_sha": resolved.sha,
            "task_image": instance.task_image,
            "task_image_id": task_image_id,
        },
    )
    run_store.create_text(
        "instance.jsonl",
        json.dumps(instance.official_row(), ensure_ascii=False, separators=(",", ":")) + "\n",
    )
    required_fail_to_pass, required_pass_to_pass = _evaluator_test_requirements(
        instance,
        evaluation_year=datetime.now(UTC).year,
    )
    evaluator_row = instance.official_row()
    evaluator_row["FAIL_TO_PASS"] = list(required_fail_to_pass)
    evaluator_row["PASS_TO_PASS"] = list(required_pass_to_pass)
    evaluator_row["fail_to_pass"] = json.dumps(required_fail_to_pass, ensure_ascii=False, separators=(",", ":"))
    evaluator_row["pass_to_pass"] = json.dumps(required_pass_to_pass, ensure_ascii=False, separators=(",", ":"))
    evaluator_copy = run_store.create_text(
        "evaluator-instance.jsonl",
        json.dumps(evaluator_row, ensure_ascii=False, separators=(",", ":")) + "\n",
    )

    evaluator = OfficialEvaluator(
        process,
        python_executable=os.fspath(config.harness_python),
        proxy=ProxyRelayConfig(config.proxy_url),
    )
    gold_prediction = run_store.create_text(
        "gold/predictions.json",
        encode_predictions(instance.instance_id, instance.patch, "gold"),
    )
    gold_patch_applied = _patch_applies(
        process,
        task_image_id=task_image_id,
        base_commit=instance.base_commit,
        patch=instance.patch,
        cwd=layout.run_artifacts,
    )
    emit_phase(RunPhase.VALIDATING_GOLD)
    gold = evaluator.evaluate(
        harness_root=config.harness_root,
        raw_sample_path=evaluator_copy,
        prediction_path=gold_prediction,
        output_dir=layout.run_artifacts / "gold" / "official",
        instance_id=instance.instance_id,
        required_fail_to_pass=required_fail_to_pass,
        required_pass_to_pass=required_pass_to_pass,
        patch_applied=gold_patch_applied,
    )

    def arms() -> tuple[OfficialEvaluation, OfficialEvaluation, Mapping[Arm, SutOutcome], dict[Arm, int]]:
        codex_secrets = auth_secret_variants(config.auth_json)
        arm_paths: dict[Arm, ArmPaths] = {}
        stores: dict[Arm, ArtifactStore] = {}
        for arm in (Arm.OFF, Arm.ON):
            arm_work = layout.arm_work(arm)
            runtime = arm_work / "runtime"
            try:
                tokensflow = snapshot_tokensflow_home(config.tokensflow_user_home, runtime / "tokensflow-home")
            except UnsafeTokensFlowConfiguration:
                raise TokensFlowInfrastructureError("TokensFlow profile snapshot failed") from None
            arm_paths[arm] = ArmPaths(
                source=materialized,
                auth_source=config.auth_json,
                workspace=arm_work / "workspace",
                runtime=runtime,
                codex_home=runtime / "codex-home",
                pc_home=runtime / "pc-home",
                result_root=layout.arm_artifacts(arm),
                tokensflow_home=tokensflow.user_home,
            )
            secrets = codex_secrets + tokensflow_secret_variants(tokensflow.credentials)
            stores[arm] = ArtifactStore(layout.arm_artifacts(arm), forbidden_values=secrets)
        prompt = instance.codex_prompt().encode()
        outcomes = DockerSut(process).run_pair(
            SutConfig(
                run_id=run_id,
                task_image=task_image_id,
                codex_binary=config.codex_binary,
                uv_binary=config.uv_binary,
                source_checkout=materialized,
                plugin_checkout_sha=resolved.sha,
                proxy=ProxyRelayConfig(config.proxy_url),
                tokensflow_binary=config.tokensflow_binary,
                tokensflow_egress_network=config.tokensflow_egress_network,
                model=config.model,
                reasoning_effort=config.reasoning_effort,
            ),
            paths=arm_paths,
            prompts={Arm.OFF: prompt, Arm.ON: prompt},
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
                encode_predictions(instance.instance_id, patch, "codex-0.145.0"),
            )
            patch_applied = _patch_applies(
                process,
                task_image_id=task_image_id,
                base_commit=instance.base_commit,
                patch=patch,
                cwd=layout.run_artifacts,
            )
            official[arm] = evaluator.evaluate(
                harness_root=config.harness_root,
                raw_sample_path=evaluator_copy,
                prediction_path=prediction,
                output_dir=layout.arm_artifacts(arm) / "official",
                instance_id=instance.instance_id,
                required_fail_to_pass=required_fail_to_pass,
                required_pass_to_pass=required_pass_to_pass,
                patch_applied=patch_applied,
            )
            write_context_trace(
                stores[arm],
                arm=arm,
                prompt=prompt,
                codex_sidecar=stores[arm].root / "context/codex-observed.jsonl",
                injection_sidecar=_optional_artifact(stores[arm].root / "context/powercontext-injections.jsonl"),
                official=official[arm],
                official_observed_at=datetime.now(UTC),
            )
        return official[Arm.OFF], official[Arm.ON], outcomes, patch_sizes

    off_eval, on_eval, outcomes, patch_sizes = run_after_gold(
        GoldResult(instance.instance_id, gold.resolved),
        arms,
    )
    off_outcome = outcomes[Arm.OFF]
    on_outcome = outcomes[Arm.ON]
    emit_phase(RunPhase.GENERATING_REPORT)
    report = ReportBundle(
        title="PowerContext Codex SWE-bench Pro comparison",
        revisions={
            "dataset": DATASET_REVISION,
            "harness": HARNESS_COMMIT,
            "powercontext": resolved.sha,
        },
        configuration={
            "codex": "0.145.0",
            "instance": instance.instance_id,
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
        },
        off=_arm_report(Arm.OFF, off_eval, off_outcome, patch_sizes[Arm.OFF]),
        on=_arm_report(Arm.ON, on_eval, on_outcome, patch_sizes[Arm.ON]),
    )
    rendered = render_report(report)
    if render_report(report) != rendered:
        raise RuntimeError("Report rendering is not deterministic")
    report_path = run_store.create_text("report.md", rendered)
    run_store.create_json("report.json", report.model_dump(mode="json"))
    return RunResult(run_id, report_path, off_eval.resolved, on_eval.resolved)


def _evaluator_test_requirements(
    instance: SweBenchProInstance,
    *,
    evaluation_year: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    # These tests parameterize two cases from datetime.now().year, so the collected node IDs advance yearly.
    replacements = {
        f"{_OPENLIBRARY_DYNAMIC_YEAR_PREFIX}[2025-True]": (
            f"{_OPENLIBRARY_DYNAMIC_YEAR_PREFIX}[{evaluation_year}-True]"
        ),
        f"{_OPENLIBRARY_DYNAMIC_YEAR_PREFIX}[2026-False]": (
            f"{_OPENLIBRARY_DYNAMIC_YEAR_PREFIX}[{evaluation_year + 1}-False]"
        ),
    }
    remapped_pass_to_pass = tuple(replacements.get(name, name) for name in instance.pass_to_pass)
    return instance.fail_to_pass, remapped_pass_to_pass


@dataclass(frozen=True)
class MinimalRunConfig:
    """Compatibility configuration for the retired one-row runner interface."""

    root: Path
    powercontext_source: Path
    powercontext_ref: str
    harness_root: Path
    harness_python: Path
    raw_sample_path: Path
    codex_binary: Path
    tokensflow_binary: Path
    tokensflow_user_home: Path
    tokensflow_egress_network: str
    uv_binary: Path
    registry_binary: Path
    auth_json: Path
    proxy_url: str
    run_id: str | None = None
    model: str = DEFAULT_CODEX_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT

    def __post_init__(self) -> None:
        if not is_safe_codex_model(self.model):
            raise ValueError("Codex model is unsafe")
        if self.reasoning_effort != DEFAULT_REASONING_EFFORT:
            raise ValueError("Unsupported Codex reasoning effort")


def run_minimal_swebench_pro(
    config: MinimalRunConfig,
    *,
    on_phase: PhaseCallback | None = None,
) -> RunResult:
    """Compatibility wrapper for an existing transformed one-row dataset."""

    raw = _read_one_jsonl(config.raw_sample_path)
    instance = SweBenchProInstance.from_raw(
        raw,
        docker_manifest_digest="sha256:" + "0" * 64,
    )
    run_id = config.run_id or datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S")
    return run_swebench_pro_instance(
        RunConfig(
            root=config.root,
            powercontext_source=config.powercontext_source,
            powercontext_ref=config.powercontext_ref,
            harness_root=config.harness_root,
            harness_python=config.harness_python,
            codex_binary=config.codex_binary,
            tokensflow_binary=config.tokensflow_binary,
            tokensflow_user_home=config.tokensflow_user_home,
            tokensflow_egress_network=config.tokensflow_egress_network,
            uv_binary=config.uv_binary,
            registry_binary=config.registry_binary,
            auth_json=config.auth_json,
            proxy_url=config.proxy_url,
            run_id=run_id,
            model=config.model,
            reasoning_effort=config.reasoning_effort,
        ),
        instance=instance,
        on_phase=on_phase,
    )


def _resolve_task_image(
    process: ProcessRunner,
    task_image: str,
    *,
    cwd: Path,
    registry_binary: Path,
    proxy_url: str,
) -> str:
    image_id = _inspect_task_image(process, task_image, cwd=cwd)
    if image_id is not None:
        return image_id

    proxy = ProxyRelayConfig(proxy_url)
    archive_prefix = f".task-image-{hashlib.sha256(task_image.encode()).hexdigest()[:16]}-"
    with tempfile.NamedTemporaryFile(prefix=archive_prefix, suffix=".tar", dir=cwd, delete=False) as stream:
        archive = Path(stream.name)
    try:
        process.run(
            (
                os.fspath(registry_binary),
                "image",
                "export",
                "--platform",
                "linux/amd64",
                "--name",
                task_image,
                task_image,
                os.fspath(archive),
            ),
            cwd=cwd,
            timeout=3_600,
            env=loopback_proxy_environment(proxy.url),
        )
        process.run(("docker", "load", "-i", os.fspath(archive)), cwd=cwd, timeout=3_600)
    finally:
        archive.unlink(missing_ok=True)

    image_id = _inspect_task_image(process, task_image, cwd=cwd)
    if image_id is None:
        raise ValueError("Imported Docker task image is unavailable")
    return image_id


def _inspect_task_image(process: ProcessRunner, task_image: str, *, cwd: Path) -> str | None:
    result = process.run(
        ("docker", "image", "inspect", "--format={{.Id}}", task_image),
        cwd=cwd,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        return None
    image_id = result.stdout.strip()
    if _IMAGE_ID.fullmatch(image_id) is None:
        raise ValueError("Docker returned an invalid immutable task image ID")
    return image_id


def _patch_applies(
    process: ProcessRunner,
    *,
    task_image_id: str,
    base_commit: str,
    patch: str,
    cwd: Path,
) -> bool:
    command = (
        f"set -e; cd /app; git reset --hard {base_commit} >/dev/null; "
        f"git checkout --detach {base_commit} >/dev/null; git apply --check -"
    )
    result = process.run(
        (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--platform",
            "linux/amd64",
            "-i",
            "--entrypoint",
            "/bin/bash",
            task_image_id,
            "-c",
            command,
        ),
        cwd=cwd,
        timeout=300,
        check=False,
        input_bytes=patch.encode(),
    )
    return result.returncode == 0


def _read_one_jsonl(path: Path) -> dict[str, object]:
    lines = path.read_text().splitlines()
    if len(lines) != 1:
        raise ValueError("Pinned raw sample must contain exactly one JSONL record")
    value = json.loads(lines[0])
    if not isinstance(value, dict):
        raise TypeError("Pinned raw sample must be a JSON object")
    return value


def _optional_artifact(path: Path) -> Path | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return path


def _arm_report(arm: Arm, evaluation: OfficialEvaluation, outcome: SutOutcome, patch_bytes: int) -> ArmReport:
    usage = outcome.codex.usage
    return ArmReport(
        arm=arm.value,
        state=ArmState.TREATMENT_VALIDATED,
        resolved=evaluation.resolved,
        passed=evaluation.resolved,
        treatment_valid=True,
        patch_applied=evaluation.patch_applied,
        fail_to_pass=TestGroupReport(
            passed=evaluation.fail_to_pass.passed,
            total=evaluation.fail_to_pass.total,
            failed=evaluation.fail_to_pass.failed,
        ),
        pass_to_pass=TestGroupReport(
            passed=evaluation.pass_to_pass.passed,
            total=evaluation.pass_to_pass.total,
            failed=evaluation.pass_to_pass.failed,
        ),
        log_excerpt=evaluation.log_excerpt,
        metrics=MetricSet(
            patch_bytes=patch_bytes,
            input_tokens=None if usage is None else usage.get("input_tokens"),
            output_tokens=None if usage is None else usage.get("output_tokens"),
        ),
    )
