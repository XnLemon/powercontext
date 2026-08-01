"""Supervised task-pair orchestration for queued evaluations."""

from __future__ import annotations

import fcntl
import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from powercontext_eval.artifacts import ArtifactError
from powercontext_eval.benchmarks.base import GoldCheckFailed
from powercontext_eval.benchmarks.swebench_pro.adapter import DatasetSchemaError, SweBenchProInstance
from powercontext_eval.benchmarks.swebench_pro.catalog import CatalogError, SweBenchProCatalog
from powercontext_eval.benchmarks.swebench_pro.evaluator import OfficialResultError
from powercontext_eval.benchmarks.swebench_pro.prediction import BinaryPatchError
from powercontext_eval.codex import CodexInfrastructureError, UnsafeCodexInvocation
from powercontext_eval.errors import CommandError, GitSourceError, PowerContextEvalError
from powercontext_eval.git_source import GitSource
from powercontext_eval.models import PowerContextRef
from powercontext_eval.paths import EvaluationPaths
from powercontext_eval.powercontext_sut import InvalidTreatment, UnsafeSutConfiguration
from powercontext_eval.process import ProcessRunner
from powercontext_eval.report import InvalidReportBundle
from powercontext_eval.runner import (
    MinimalRunConfig,
    MinimalRunResult,
    RunConfig,
    RunPhase,
    run_minimal_swebench_pro,
    run_swebench_pro_instance,
)
from powercontext_eval.web.claiming import ClaimCoordinator
from powercontext_eval.web.config import WebConfig
from powercontext_eval.web.controls import BatchPauseReason
from powercontext_eval.web.models import FailureCategory, SafeFailure, TaskPhase, TaskRecord, TaskResult
from powercontext_eval.web.reporting import ReportingError, load_report
from powercontext_eval.web.store import TaskConflict, TaskOwnershipError, TaskStore
from powercontext_eval.web.usage import CodexUsageProbe, UsageSnapshot

_INTERNAL_SUMMARY = "The evaluation worker failed unexpectedly. Inspect the retained m0 logs."
_REPORT_SUMMARY = "Evaluation report validation failed."


class ThreadLike(Protocol):
    def start(self) -> None: ...

    def join(self) -> None: ...


ThreadFactory = Callable[..., ThreadLike]
Runner = Callable[..., MinimalRunResult]


class SourceResolver(Protocol):
    def resolve(self, source: str | Path, requested: PowerContextRef) -> object: ...


class Catalog(Protocol):
    def require(self, instance_id: str) -> SweBenchProInstance: ...


class UsageProbe(Protocol):
    def read(self, *, now: datetime) -> UsageSnapshot: ...


class TaskPairWorker:
    """Claim and execute complete OFF/ON task pairs in one isolated slot."""

    def __init__(
        self,
        config: WebConfig,
        store: TaskStore,
        *,
        coordinator: ClaimCoordinator | None = None,
        usage_probe: UsageProbe | None = None,
        runner: Runner | None = None,
        source: SourceResolver | None = None,
        catalog: Catalog | None = None,
        worker_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        thread_factory: ThreadFactory = threading.Thread,
    ) -> None:
        self._config = config
        self._store = store
        self._usage_probe = usage_probe or CodexUsageProbe(
            codex_binary=config.codex_binary,
            auth_json=config.auth_json,
            proxy_url=config.proxy_url,
            timeout_seconds=config.usage_probe_timeout_seconds,
        )
        self._batch_runner: Runner = runner or run_swebench_pro_instance
        self._legacy_runner: Runner = runner or run_minimal_swebench_pro
        self._source = source or GitSource(
            cache_root=config.run_root / "cache" / "powercontext-git",
            runner=ProcessRunner(),
        )
        self._catalog = catalog
        self._worker_id = worker_id or f"worker-{uuid4().hex}"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._coordinator = coordinator or ClaimCoordinator(
            config, store, usage_probe=self._usage_probe, clock=self._clock
        )
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._thread_factory = thread_factory

    def stop(self) -> None:
        """Request shutdown after the active evaluation returns."""
        self._coordinator.stop()
        self._stop.set()

    def run_once(self) -> bool:
        """Run the next task, returning whether one was claimed."""
        task = self._coordinator.claim(self._worker_id)
        if task is None:
            return False
        return self._run_claimed(task)

    def _run_claimed(self, task: TaskRecord) -> bool:
        ownership_lost = threading.Event()
        heartbeat_stop = threading.Event()
        heartbeat: ThreadLike | None = None
        heartbeat_started = False
        phase: TaskPhase | None = None
        attempt_finished = False
        try:
            heartbeat = self._thread_factory(
                target=self._heartbeat,
                daemon=True,
                name=f"evaluation-heartbeat-{task.task_id}",
                args=(task.task_id, heartbeat_stop, ownership_lost),
            )
            heartbeat.start()
            heartbeat_started = True
            run_id = _execution_run_id(task)
            layout = EvaluationPaths(self._config.run_root, run_id)
            work_dir = self._config.run_root / "work" / run_id
            if os.path.lexists(layout.run_artifacts) or os.path.lexists(work_dir):
                attempt_finished = self._fail(
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

            result = self._invoke_runner(task, on_phase)
            if ownership_lost.is_set():
                return True
            task_result = self._validated_result(task, result)
            load_report(layout.run_artifacts, self._config.run_root / "runs")
            self._store.succeed(task.task_id, self._worker_id, task_result, now=self._clock())
            attempt_finished = True
        except (TaskOwnershipError, TaskConflict):
            ownership_lost.set()
        except Exception as error:  # noqa: BLE001 - the worker boundary must sanitize every runner failure
            attempt_finished = self._fail(task, _safe_failure(error, phase), ownership_lost)
        finally:
            heartbeat_stop.set()
            if heartbeat_started and heartbeat is not None:
                heartbeat.join()
            if attempt_finished and task.batch_id is not None and not ownership_lost.is_set():
                self._finalize_batch(task.batch_id)
        return True

    def run_forever(self, stop: threading.Event | None = None) -> None:
        """Poll until stopped, recovering an expired predecessor before each claim."""
        stop_event = stop or self._stop
        while not stop_event.is_set():
            if not self.run_once():
                self._sleep(self._config.poll_seconds)

    def _invoke_runner(self, task: TaskRecord, on_phase: Callable[[RunPhase], None]) -> MinimalRunResult:
        if task.batch_id is None:
            return self._legacy_runner(self._legacy_run_config(task), on_phase=on_phase)
        if task.instance_id is None:
            raise DatasetSchemaError("Batch child is missing an instance ID")
        catalog = self._catalog
        if catalog is None:
            catalog = SweBenchProCatalog.load(self._config.dataset_path)
            self._catalog = catalog
        return self._batch_runner(
            self._batch_run_config(task),
            instance=catalog.require(task.instance_id),
            on_phase=on_phase,
        )

    def _batch_run_config(self, task: TaskRecord) -> RunConfig:
        if task.batch_id is None:
            raise ValueError("Batch run configuration requires a batch child")
        powercontext_ref = self._pinned_batch_ref(task.batch_id)
        return RunConfig(
            root=self._config.run_root,
            powercontext_source=self._config.powercontext_source,
            powercontext_ref=powercontext_ref,
            harness_root=self._config.harness_root,
            harness_python=self._config.harness_python,
            codex_binary=self._config.codex_binary,
            uv_binary=self._config.uv_binary,
            registry_binary=self._config.registry_binary,
            auth_json=self._config.auth_json,
            proxy_url=self._config.proxy_url,
            run_id=_execution_run_id(task),
        )

    def _legacy_run_config(self, task: TaskRecord) -> MinimalRunConfig:
        return MinimalRunConfig(
            root=self._config.run_root,
            powercontext_source=self._config.powercontext_source,
            powercontext_ref=task.request.powercontext_ref,
            harness_root=self._config.harness_root,
            harness_python=self._config.harness_python,
            raw_sample_path=self._config.raw_sample_path,
            codex_binary=self._config.codex_binary,
            uv_binary=self._config.uv_binary,
            registry_binary=self._config.registry_binary,
            auth_json=self._config.auth_json,
            proxy_url=self._config.proxy_url,
            run_id=_execution_run_id(task),
        )

    def _pinned_batch_ref(self, batch_id: str) -> str:
        batch = self._store.get_batch(batch_id)
        if batch.resolved_powercontext_sha is not None:
            return f"commit:{batch.resolved_powercontext_sha}"
        requested = PowerContextRef.parse(batch.request.powercontext_ref)
        if requested.kind == "commit":
            assert requested.value is not None
            sha = requested.value.lower()
        else:
            resolved = self._source.resolve(self._config.powercontext_source, requested)
            sha = getattr(resolved, "sha", None)
            if not isinstance(sha, str):
                raise GitSourceError("PowerContext source resolver returned no immutable SHA")
        try:
            pinned = self._store.pin_batch_revision(batch_id, sha)
        except TaskConflict as error:
            raise GitSourceError("PowerContext batch revision conflicted with its persisted pin") from error
        assert pinned.resolved_powercontext_sha is not None
        return f"commit:{pinned.resolved_powercontext_sha}"

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
        run_id = _execution_run_id(task)
        layout = EvaluationPaths(self._config.run_root, run_id)
        if result.run_id != run_id:
            raise InvalidReportBundle("Runner returned a mismatched run ID")
        expected_report = layout.run_artifacts / "report.md"
        try:
            if result.report_path != expected_report:
                raise InvalidReportBundle("Runner returned an unexpected report path")
            metadata = expected_report.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise InvalidReportBundle("Runner report is not a regular file")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(expected_report, flags)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise InvalidReportBundle("Runner report changed during validation")
            finally:
                os.close(descriptor)
        except (FileNotFoundError, OSError, RuntimeError, ValueError, InvalidReportBundle):
            raise InvalidReportBundle("Runner returned an unsafe report path") from None
        return TaskResult(
            artifact_dir=os.fspath(layout.run_artifacts.relative_to(self._config.run_root)),
            report_path=os.fspath(expected_report.relative_to(self._config.run_root)),
            off_resolved=result.off_resolved,
            on_resolved=result.on_resolved,
        )

    def _fail(self, task: TaskRecord, failure: SafeFailure, ownership_lost: threading.Event) -> bool:
        if ownership_lost.is_set():
            return False
        try:
            self._store.fail(task.task_id, self._worker_id, failure, now=self._clock())
        except (TaskOwnershipError, TaskConflict):
            ownership_lost.set()
            return False
        return True

    def _finalize_batch(self, batch_id: str) -> None:
        self._coordinator.refresh_after_attempt(batch_id)


class EvaluationWorker:
    """Own one Worker process and supervise configured task-pair slots."""

    def __init__(
        self,
        config: WebConfig,
        store: TaskStore,
        *,
        usage_probe: UsageProbe | None = None,
        runner: Runner | None = None,
        source: SourceResolver | None = None,
        catalog: Catalog | None = None,
        worker_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        thread_factory: ThreadFactory = threading.Thread,
    ) -> None:
        self._config = config
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event()
        shared_probe = usage_probe or CodexUsageProbe(
            codex_binary=config.codex_binary,
            auth_json=config.auth_json,
            proxy_url=config.proxy_url,
            timeout_seconds=config.usage_probe_timeout_seconds,
        )
        coordinator = ClaimCoordinator(
            config,
            store,
            usage_probe=shared_probe,
            clock=self._clock,
        )
        self._coordinator = coordinator
        base_worker_id = worker_id or f"worker-{uuid4().hex}"

        def default_sleep(seconds: float) -> None:
            self._stop.wait(seconds)

        self._slots = tuple(
            TaskPairWorker(
                config,
                store,
                coordinator=coordinator,
                usage_probe=shared_probe,
                runner=runner,
                source=source,
                catalog=catalog,
                worker_id=(base_worker_id if config.task_parallelism == 1 else f"{base_worker_id}-slot-{index + 1}"),
                clock=self._clock,
                sleep=sleep or default_sleep,
                thread_factory=thread_factory,
            )
            for index in range(config.task_parallelism)
        )

    def stop(self) -> None:
        """Stop claiming replacements after active task pairs finish."""

        self._coordinator.stop()
        self._stop.set()

    def run_once(self) -> bool:
        """Run one slot once while retaining the process-owner compatibility boundary."""

        with _nonblocking_worker_lock(self._config.database_path) as locked:
            if not locked:
                return False
            return self._slots[0].run_once()

    def run_forever(self) -> None:
        """Own the process lock and supervise all configured task-pair slots."""

        with _nonblocking_worker_lock(self._config.database_path) as locked:
            if not locked:
                return
            self._store.record_worker_capacity(self._config.task_parallelism, now=self._clock())
            failures: list[BaseException] = []
            failures_lock = threading.Lock()

            def pause_after_slot_failure() -> None:
                try:
                    self._store.pause_runnable_batches(
                        reason=BatchPauseReason.INFRASTRUCTURE_FAILURE,
                        now=self._clock(),
                    )
                except Exception as error:  # noqa: BLE001 - preserve the original supervisor failure too
                    with failures_lock:
                        failures.append(error)

            def run_slot(slot: TaskPairWorker) -> None:
                try:
                    slot.run_forever(self._stop)
                except Exception as error:  # noqa: BLE001 - a slot failure must stop the whole supervisor
                    with failures_lock:
                        failures.append(error)
                    self.stop()
                    pause_after_slot_failure()

            threads = tuple(
                threading.Thread(
                    target=run_slot,
                    args=(slot,),
                    daemon=False,
                    name=f"evaluation-slot-{index + 1}",
                )
                for index, slot in enumerate(self._slots)
            )
            started: list[threading.Thread] = []
            try:
                for thread in threads:
                    thread.start()
                    started.append(thread)
            except Exception:  # noqa: BLE001 - partial startup must stop and join every started slot
                self.stop()
                pause_after_slot_failure()
                for thread in started:
                    thread.join()
                raise RuntimeError("Evaluation worker slot failed") from None
            for thread in started:
                thread.join()
            if failures:
                raise RuntimeError("Evaluation worker slot failed") from None


def _execution_run_id(task: TaskRecord) -> str:
    if task.attempt_number == 1:
        return task.task_id
    if task.attempt_id is None:
        raise ValueError("Retried task is missing an attempt ID")
    expected_attempt_id = f"{task.task_id}.attempt-{task.attempt_number:04d}"
    if task.attempt_id != expected_attempt_id:
        raise ValueError("Retried task has an invalid attempt ID")
    return f"{task.task_id}-attempt-{task.attempt_number:04d}"


@contextmanager
def _nonblocking_worker_lock(database_path: Path) -> Iterator[bool]:
    lock_path = database_path.with_name(f"{database_path.name}.worker.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        metadata = lock_path.lstat()
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino)
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RuntimeError("Evaluation worker lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        locked = True
        yield True
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _safe_failure(error: Exception, phase: TaskPhase | None) -> SafeFailure:
    fixed: tuple[FailureCategory, str]
    if isinstance(error, GitSourceError):
        fixed = FailureCategory.SOURCE_RESOLUTION, "PowerContext source resolution failed."
    elif isinstance(error, (CatalogError, DatasetSchemaError, UnsafeSutConfiguration)):
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
