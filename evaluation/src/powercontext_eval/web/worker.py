"""Serial orchestration for queued evaluation tasks."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from powercontext_eval.artifacts import ArtifactError
from powercontext_eval.benchmarks.base import GoldCheckFailed
from powercontext_eval.benchmarks.swebench_pro.adapter import DatasetSchemaError
from powercontext_eval.benchmarks.swebench_pro.evaluator import OfficialResultError
from powercontext_eval.benchmarks.swebench_pro.prediction import BinaryPatchError
from powercontext_eval.codex import CodexInfrastructureError, UnsafeCodexInvocation
from powercontext_eval.errors import CommandError, GitSourceError, PowerContextEvalError
from powercontext_eval.paths import EvaluationPaths
from powercontext_eval.powercontext_sut import InvalidTreatment, UnsafeSutConfiguration
from powercontext_eval.report import InvalidReportBundle
from powercontext_eval.runner import (
    MinimalRunConfig,
    MinimalRunResult,
    RunPhase,
    run_minimal_swebench_pro,
)
from powercontext_eval.web.config import WebConfig
from powercontext_eval.web.models import FailureCategory, SafeFailure, TaskPhase, TaskRecord, TaskResult
from powercontext_eval.web.reporting import ReportingError, load_report
from powercontext_eval.web.store import TaskConflict, TaskOwnershipError, TaskStore

_INTERNAL_SUMMARY = "The evaluation worker failed unexpectedly. Inspect the retained m0 logs."
_REPORT_SUMMARY = "Evaluation report validation failed."


class ThreadLike(Protocol):
    def start(self) -> None: ...

    def join(self) -> None: ...


ThreadFactory = Callable[..., ThreadLike]
Runner = Callable[..., MinimalRunResult]


class EvaluationWorker:
    """Claim and execute at most one queued evaluation at a time."""

    def __init__(
        self,
        config: WebConfig,
        store: TaskStore,
        *,
        runner: Runner = run_minimal_swebench_pro,
        worker_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        thread_factory: ThreadFactory = threading.Thread,
    ) -> None:
        self._config = config
        self._store = store
        self._runner = runner
        self._worker_id = worker_id or f"worker-{uuid4().hex}"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._thread_factory = thread_factory

    def stop(self) -> None:
        """Request shutdown after the active evaluation returns."""
        self._stop.set()

    def run_once(self) -> bool:
        """Run the next task, returning whether one was claimed."""
        task = self._store.claim_next(self._worker_id, now=self._clock())
        if task is None:
            return False

        ownership_lost = threading.Event()
        heartbeat_stop = threading.Event()
        heartbeat = self._thread_factory(
            target=self._heartbeat,
            daemon=True,
            name=f"evaluation-heartbeat-{task.task_id}",
            args=(task.task_id, heartbeat_stop, ownership_lost),
        )
        heartbeat.start()
        phase: TaskPhase | None = None
        try:
            layout = EvaluationPaths(self._config.run_root, task.task_id)
            work_dir = self._config.run_root / "work" / task.task_id
            if os.path.lexists(layout.run_artifacts) or os.path.lexists(work_dir):
                self._fail(
                    task,
                    SafeFailure(
                        category=FailureCategory.REPORT_GENERATION,
                        summary="Evaluation artifacts already exist; refusing to overwrite them.",
                    ),
                    ownership_lost,
                )
                return True

            def on_phase(run_phase: RunPhase) -> None:
                nonlocal phase
                mapped = TaskPhase(run_phase.value)
                try:
                    self._store.set_phase(task.task_id, self._worker_id, mapped, now=self._clock())
                except (TaskOwnershipError, TaskConflict):
                    ownership_lost.set()
                    return
                phase = mapped

            result = self._runner(self._run_config(task), on_phase=on_phase)
            if ownership_lost.is_set():
                return True
            task_result = self._validated_result(task, result)
            load_report(layout.run_artifacts, self._config.run_root / "runs")
            self._store.succeed(task.task_id, self._worker_id, task_result, now=self._clock())
        except (TaskOwnershipError, TaskConflict):
            ownership_lost.set()
        except Exception as error:  # noqa: BLE001 - the worker boundary must sanitize every runner failure
            self._fail(task, _safe_failure(error, phase), ownership_lost)
        finally:
            heartbeat_stop.set()
            heartbeat.join()
        return True

    def run_forever(self) -> None:
        """Recover a dead predecessor once, then poll until stopped."""
        self._store.recover_expired(now=self._clock())
        while not self._stop.is_set():
            if not self.run_once():
                self._sleep(self._config.poll_seconds)

    def _run_config(self, task: TaskRecord) -> MinimalRunConfig:
        return MinimalRunConfig(
            root=self._config.run_root,
            powercontext_source=self._config.powercontext_source,
            powercontext_ref=task.request.powercontext_ref,
            harness_root=self._config.harness_root,
            harness_python=self._config.harness_python,
            raw_sample_path=self._config.raw_sample_path,
            codex_binary=self._config.codex_binary,
            uv_binary=self._config.uv_binary,
            auth_json=self._config.auth_json,
            proxy_url=self._config.proxy_url,
            run_id=task.task_id,
        )

    def _heartbeat(
        self,
        task_id: str,
        stop: threading.Event,
        ownership_lost: threading.Event,
    ) -> None:
        interval = self._config.lease_seconds / 3
        while not stop.wait(interval):
            try:
                self._store.heartbeat(task_id, self._worker_id, now=self._clock())
            except (TaskOwnershipError, TaskConflict):
                ownership_lost.set()
                return
            except Exception:  # noqa: BLE001 - a failed renewal makes later task mutation unsafe
                ownership_lost.set()
                return

    def _validated_result(self, task: TaskRecord, result: MinimalRunResult) -> TaskResult:
        layout = EvaluationPaths(self._config.run_root, task.task_id)
        if result.run_id != task.task_id:
            raise InvalidReportBundle("Runner returned a mismatched run ID")
        try:
            run_dir = layout.run_artifacts.resolve(strict=True)
            report = result.report_path.resolve(strict=True)
            report.relative_to(run_dir)
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            raise InvalidReportBundle("Runner returned an unsafe report path") from None
        return TaskResult(
            artifact_dir=os.fspath(layout.run_artifacts.relative_to(self._config.run_root)),
            report_path=os.fspath(report.relative_to(self._config.run_root.resolve())),
            off_resolved=result.off_resolved,
            on_resolved=result.on_resolved,
        )

    def _fail(self, task: TaskRecord, failure: SafeFailure, ownership_lost: threading.Event) -> None:
        if ownership_lost.is_set():
            return
        try:
            self._store.fail(task.task_id, self._worker_id, failure, now=self._clock())
        except (TaskOwnershipError, TaskConflict):
            ownership_lost.set()


def _safe_failure(error: Exception, phase: TaskPhase | None) -> SafeFailure:
    fixed: tuple[FailureCategory, str]
    if isinstance(error, GitSourceError):
        fixed = FailureCategory.SOURCE_RESOLUTION, "PowerContext source resolution failed."
    elif isinstance(error, (DatasetSchemaError, UnsafeSutConfiguration)):
        fixed = FailureCategory.ENVIRONMENT_PREPARATION, "Evaluation environment preparation failed."
    elif isinstance(error, GoldCheckFailed):
        fixed = FailureCategory.GOLD_VALIDATION, "Gold patch validation failed."
    elif isinstance(error, (CodexInfrastructureError, UnsafeCodexInvocation, BinaryPatchError)):
        fixed = FailureCategory.CODEX_EXECUTION, "Codex execution failed."
    elif isinstance(error, InvalidTreatment):
        fixed = FailureCategory.TREATMENT_VALIDATION, "Treatment validation failed."
    elif isinstance(error, OfficialResultError):
        fixed = FailureCategory.OFFICIAL_EVALUATOR, "Official evaluation failed."
    elif isinstance(error, (ReportingError, InvalidReportBundle, ArtifactError)):
        fixed = FailureCategory.REPORT_GENERATION, _REPORT_SUMMARY
    elif isinstance(error, (CommandError, PowerContextEvalError)):
        fixed = _phase_failure(phase)
    else:
        fixed = FailureCategory.INTERNAL, _INTERNAL_SUMMARY
    return SafeFailure(category=fixed[0], phase=phase, summary=fixed[1])


def _phase_failure(phase: TaskPhase | None) -> tuple[FailureCategory, str]:
    return {
        TaskPhase.PREPARING: (FailureCategory.ENVIRONMENT_PREPARATION, "Evaluation environment preparation failed."),
        TaskPhase.VALIDATING_GOLD: (FailureCategory.GOLD_VALIDATION, "Gold patch validation failed."),
        TaskPhase.RUNNING_OFF: (FailureCategory.CODEX_EXECUTION, "Codex execution failed."),
        TaskPhase.RUNNING_ON: (FailureCategory.CODEX_EXECUTION, "Codex execution failed."),
        TaskPhase.OFFICIAL_EVALUATION: (FailureCategory.OFFICIAL_EVALUATOR, "Official evaluation failed."),
        TaskPhase.GENERATING_REPORT: (FailureCategory.REPORT_GENERATION, _REPORT_SUMMARY),
        None: (FailureCategory.INTERNAL, _INTERNAL_SUMMARY),
    }[phase]
