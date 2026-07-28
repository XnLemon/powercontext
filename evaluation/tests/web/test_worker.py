from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from powercontext_eval.benchmarks.base import GoldCheckFailed
from powercontext_eval.benchmarks.swebench_pro.adapter import DatasetSchemaError
from powercontext_eval.benchmarks.swebench_pro.evaluator import OfficialResultError
from powercontext_eval.codex import CodexInfrastructureError
from powercontext_eval.errors import GitSourceError
from powercontext_eval.powercontext_sut import InvalidTreatment, UnsafeSutConfiguration
from powercontext_eval.runner import MinimalRunResult, RunPhase
from powercontext_eval.web.config import WebConfig
from powercontext_eval.web.models import FailureCategory, TaskCreate, TaskPhase, TaskStatus
from powercontext_eval.web.store import TaskStore
from powercontext_eval.web.worker import EvaluationWorker

NOW = datetime(2026, 7, 29, 1, 2, 3, tzinfo=UTC)


def _config(root: Path, *, lease_seconds: int = 2, poll_seconds: float = 0.01) -> WebConfig:
    return WebConfig.for_root(
        root,
        run_root=root / "artifacts",
        powercontext_source=root / "source",
        harness_root=root / "harness",
        harness_python=root / "venv/bin/python",
        raw_sample_path=root / "sample.jsonl",
        codex_binary=root / "bin/codex",
        uv_binary=root / "bin/uv",
        auth_json=root / "codex/auth.json",
        proxy_url="http://127.0.0.1:7890",
        lease_seconds=lease_seconds,
        poll_seconds=poll_seconds,
    )


def _store(config: WebConfig) -> TaskStore:
    store = TaskStore(config.database_path, lease_duration=timedelta(seconds=config.lease_seconds))
    store.initialize()
    return store


def _create(store: TaskStore, *, key: str = "worker-test", now: datetime = NOW) -> Any:
    return store.create(
        TaskCreate(
            powercontext_ref="commit:" + "a" * 40,
            benchmark="swebench-pro",
            instance_id="instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            treatment_mode="off_on",
            idempotency_key=key,
        ),
        now=now,
    )[0]


def test_run_once_without_work_returns_false(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def runner(config: Any, *, on_phase: Any) -> MinimalRunResult:
        pytest.fail("runner should not run")

    worker = EvaluationWorker(config, _store(config), runner=runner, clock=lambda: NOW)

    assert worker.run_once() is False


def test_run_once_maps_config_phases_and_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    task = _create(store)
    calls = []
    observed = []

    def runner(run_config: Any, *, on_phase: Any) -> MinimalRunResult:
        calls.append(run_config)
        for phase in RunPhase:
            before = store.get(task.task_id).version
            on_phase(phase)
            current = store.get(task.task_id)
            observed.append((current.phase, current.version > before))
        run_dir = config.run_root / "runs" / task.task_id
        run_dir.mkdir(parents=True)
        report_path = run_dir / "report.md"
        report_path.write_text("safe")
        return MinimalRunResult(task.task_id, report_path, True, False)

    loaded = []
    monkeypatch.setattr(
        "powercontext_eval.web.worker.load_report",
        lambda run_dir, run_root: loaded.append((run_dir, run_root)) or object(),
    )
    worker = EvaluationWorker(config, store, runner=runner, clock=lambda: NOW)

    assert worker.run_once() is True
    mapped = calls[0]
    assert mapped.run_id == task.task_id
    assert mapped.root == config.run_root
    assert mapped.powercontext_source == config.powercontext_source
    assert mapped.powercontext_ref == task.request.powercontext_ref
    assert mapped.harness_root == config.harness_root
    assert mapped.harness_python == config.harness_python
    assert mapped.raw_sample_path == config.raw_sample_path
    assert mapped.codex_binary == config.codex_binary
    assert mapped.uv_binary == config.uv_binary
    assert mapped.auth_json == config.auth_json
    assert mapped.proxy_url == config.proxy_url
    assert observed == [(TaskPhase(phase.value), True) for phase in RunPhase]
    assert loaded == [(config.run_root / "runs" / task.task_id, config.run_root / "runs")]
    completed = store.get(task.task_id)
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.result is not None
    assert completed.result.artifact_dir == f"runs/{task.task_id}"
    assert completed.result.report_path == f"runs/{task.task_id}/report.md"
    assert (completed.result.off_resolved, completed.result.on_resolved) == (True, False)


def test_only_one_worker_can_claim_a_task(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    task = _create(store, now=datetime.now(UTC))
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def runner(run_config: Any, *, on_phase: Any) -> MinimalRunResult:
        calls.append(run_config.run_id)
        entered.set()
        release.wait(timeout=2)
        raise RuntimeError("stop")

    first = EvaluationWorker(config, store, runner=runner, worker_id="first")
    second = EvaluationWorker(config, store, runner=runner, worker_id="second")
    thread = threading.Thread(target=first.run_once)
    thread.start()
    assert entered.wait(timeout=2)
    assert second.run_once() is False
    release.set()
    thread.join(timeout=2)
    assert calls == [task.task_id]


@pytest.mark.parametrize(
    ("error", "category", "summary"),
    [
        (GitSourceError("secret"), FailureCategory.SOURCE_RESOLUTION, "PowerContext source resolution failed."),
        (
            UnsafeSutConfiguration("secret"),
            FailureCategory.ENVIRONMENT_PREPARATION,
            "Evaluation environment preparation failed.",
        ),
        (
            DatasetSchemaError("secret"),
            FailureCategory.ENVIRONMENT_PREPARATION,
            "Evaluation environment preparation failed.",
        ),
        (GoldCheckFailed("secret"), FailureCategory.GOLD_VALIDATION, "Gold patch validation failed."),
        (CodexInfrastructureError("secret"), FailureCategory.CODEX_EXECUTION, "Codex execution failed."),
        (InvalidTreatment("secret"), FailureCategory.TREATMENT_VALIDATION, "Treatment validation failed."),
        (OfficialResultError("secret"), FailureCategory.OFFICIAL_EVALUATOR, "Official evaluation failed."),
    ],
)
def test_known_failures_have_fixed_safe_mapping(
    tmp_path: Path,
    error: Exception,
    category: FailureCategory,
    summary: str,
) -> None:
    config = _config(tmp_path)
    store = _store(config)
    task = _create(store)

    def runner(config: Any, *, on_phase: Any) -> MinimalRunResult:
        raise error

    assert EvaluationWorker(config, store, runner=runner, clock=lambda: NOW).run_once() is True
    failed = store.get(task.task_id)
    assert failed.failure_category is category
    assert failed.failure_summary == summary


def test_unknown_failure_never_persists_exception_text(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    task = _create(store)
    credential = "sk-fake-credential"

    def runner(config: Any, *, on_phase: Any) -> MinimalRunResult:
        on_phase(RunPhase.RUNNING_ON)
        raise RuntimeError(f"proxy had {credential}")

    EvaluationWorker(config, store, runner=runner, clock=lambda: NOW).run_once()
    failed = store.get(task.task_id)
    assert failed.failure_category is FailureCategory.INTERNAL
    assert failed.failure_phase is TaskPhase.RUNNING_ON
    assert failed.failure_summary == "The evaluation worker failed unexpectedly. Inspect the retained m0 logs."
    assert credential not in config.database_path.read_bytes().decode(errors="ignore")


def test_existing_artifacts_fail_before_runner_without_overwrite(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    task = _create(store)
    run_dir = config.run_root / "runs" / task.task_id
    run_dir.mkdir(parents=True)
    marker = run_dir / "keep.txt"
    marker.write_text("original")
    calls = []

    def runner(config: Any, *, on_phase: Any) -> MinimalRunResult:
        calls.append(config)
        pytest.fail("runner should not run")

    worker = EvaluationWorker(config, store, runner=runner, clock=lambda: NOW)

    assert worker.run_once() is True
    failed = store.get(task.task_id)
    assert failed.failure_category is FailureCategory.REPORT_GENERATION
    assert failed.failure_summary == "Evaluation artifacts already exist; refusing to overwrite them."
    assert marker.read_text() == "original"
    assert calls == []


def test_heartbeat_keeps_lease_alive_during_blocking_runner(tmp_path: Path) -> None:
    config = _config(tmp_path, lease_seconds=1)
    store = _store(config)
    task = _create(store, now=datetime.now(UTC))
    entered = threading.Event()
    release = threading.Event()

    def runner(run_config: Any, *, on_phase: Any) -> MinimalRunResult:
        entered.set()
        assert release.wait(timeout=3)
        raise RuntimeError("done")

    worker = EvaluationWorker(config, store, runner=runner)
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert entered.wait(timeout=2)
    time.sleep(1.2)
    assert store.recover_expired(now=datetime.now(UTC)) == []
    assert store.get(task.task_id).status is TaskStatus.RUNNING
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


class RecordingThread:
    def __init__(self, *, target: Any, daemon: bool, name: str, args: tuple[Any, ...]) -> None:
        self.target = target
        self.daemon = daemon
        self.name = name
        self.args = args
        self.started = False
        self.joined = False

    def start(self) -> None:
        self.started = True

    def join(self) -> None:
        self.joined = True


class StartFailingThread(RecordingThread):
    def start(self) -> None:
        raise RuntimeError("thread start leaked-secret")


@pytest.mark.parametrize("succeeds", [True, False])
def test_heartbeat_thread_stops_and_joins(succeeds: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    task = _create(store)
    threads = []

    def runner(run_config: Any, *, on_phase: Any) -> MinimalRunResult:
        if not succeeds:
            raise RuntimeError("failure")
        run_dir = config.run_root / "runs" / task.task_id
        run_dir.mkdir(parents=True)
        path = run_dir / "report.md"
        path.write_text("report")
        return MinimalRunResult(task.task_id, path, True, True)

    monkeypatch.setattr("powercontext_eval.web.worker.load_report", lambda *args: object())

    def thread_factory(**kwargs: Any) -> RecordingThread:
        thread = RecordingThread(**kwargs)
        threads.append(thread)
        return thread

    EvaluationWorker(config, store, runner=runner, thread_factory=thread_factory, clock=lambda: NOW).run_once()
    assert len(threads) == 1
    assert threads[0].started
    assert threads[0].joined
    assert threads[0].args[1].is_set()


def test_heartbeat_start_failure_is_safely_persisted_and_run_forever_continues(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    task = _create(store)
    waits = []
    runner_calls = []
    worker: EvaluationWorker

    def runner(config: Any, *, on_phase: Any) -> MinimalRunResult:
        runner_calls.append(config)
        pytest.fail("runner should not run")

    def thread_factory(**kwargs: Any) -> StartFailingThread:
        return StartFailingThread(**kwargs)

    def wait(seconds: float) -> None:
        waits.append(seconds)
        worker.stop()

    worker = EvaluationWorker(
        config,
        store,
        runner=runner,
        thread_factory=thread_factory,
        clock=lambda: NOW,
        sleep=wait,
    )
    worker.run_forever()

    failed = store.get(task.task_id)
    assert failed.status is TaskStatus.FAILED
    assert failed.failure_category is FailureCategory.INTERNAL
    assert failed.failure_summary == "The evaluation worker failed unexpectedly. Inspect the retained m0 logs."
    assert runner_calls == []
    assert waits == [config.poll_seconds]


def test_ownership_loss_prevents_stale_worker_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path, lease_seconds=1)
    store = _store(config)
    task = _create(store)
    later = NOW + timedelta(seconds=2)

    def runner(run_config: Any, *, on_phase: Any) -> MinimalRunResult:
        assert store.recover_expired(now=later) == [task.task_id]
        on_phase(RunPhase.RUNNING_ON)
        return MinimalRunResult(task.task_id, config.run_root / "runs" / task.task_id / "report.md", True, True)

    times = iter((NOW, later, later))
    assert EvaluationWorker(config, store, runner=runner, clock=lambda: next(times)).run_once() is True
    record = store.get(task.task_id)
    assert record.status is TaskStatus.INTERRUPTED
    assert record.phase is None
    assert record.result is None


def test_host_lock_prevents_recovery_and_second_runner_until_stale_process_releases(tmp_path: Path) -> None:
    config = _config(tmp_path, lease_seconds=1)
    store = _store(config)
    first_task = _create(store, key="first-task")
    second_task = _create(store, key="second-task")
    later = NOW + timedelta(seconds=2)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def stale_runner(run_config: Any, *, on_phase: Any) -> MinimalRunResult:
        calls.append(("first", run_config.run_id))
        entered.set()
        assert release.wait(timeout=2)
        raise RuntimeError("stale runner returned")

    def successor_runner(run_config: Any, *, on_phase: Any) -> MinimalRunResult:
        calls.append(("second", run_config.run_id))
        raise RuntimeError("successor ran")

    def no_heartbeat(**kwargs: Any) -> RecordingThread:
        return RecordingThread(**kwargs)

    first_times = iter((NOW, later))
    first = EvaluationWorker(
        config,
        store,
        runner=stale_runner,
        worker_id="first",
        clock=lambda: next(first_times),
        thread_factory=no_heartbeat,
    )
    successor = EvaluationWorker(
        config,
        store,
        runner=successor_runner,
        worker_id="successor",
        clock=lambda: later,
        sleep=lambda seconds: successor.stop(),
    )
    thread = threading.Thread(target=first.run_once)
    thread.start()
    assert entered.wait(timeout=2)

    successor.run_forever()
    assert store.get(first_task.task_id).status is TaskStatus.RUNNING
    assert calls == [("first", first_task.task_id)]

    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert successor.run_once() is True
    assert store.get(first_task.task_id).status is TaskStatus.INTERRUPTED
    assert calls == [("first", first_task.task_id), ("second", second_task.task_id)]


@pytest.mark.parametrize(
    "result",
    [
        MinimalRunResult("wrong-run", Path("/tmp/report.md"), True, True),
        MinimalRunResult("placeholder", Path("/tmp/outside.md"), True, True),
    ],
)
def test_invalid_runner_result_fails_safely(
    result: MinimalRunResult,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = _store(config)
    task = _create(store)
    actual = (
        result
        if result.run_id == "wrong-run"
        else MinimalRunResult(task.task_id, config.run_root / "outside.md", True, True)
    )

    assert EvaluationWorker(config, store, runner=lambda *args, **kwargs: actual, clock=lambda: NOW).run_once() is True
    failed = store.get(task.task_id)
    assert failed.failure_category is FailureCategory.REPORT_GENERATION
    assert failed.failure_summary == "Evaluation report validation failed."


@pytest.mark.parametrize("invalid_kind", ["directory", "unrelated", "symlink"])
def test_runner_report_must_be_exact_regular_canonical_report(
    invalid_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = _store(config)
    task = _create(store)

    def runner(run_config: Any, *, on_phase: Any) -> MinimalRunResult:
        run_dir = config.run_root / "runs" / task.task_id
        run_dir.mkdir(parents=True)
        expected = run_dir / "report.md"
        if invalid_kind == "directory":
            expected.mkdir()
            returned = expected
        elif invalid_kind == "unrelated":
            returned = run_dir / "other.md"
            returned.write_text("other")
        else:
            target = run_dir / "actual.md"
            target.write_text("actual")
            expected.symlink_to(target)
            returned = expected
        return MinimalRunResult(task.task_id, returned, True, True)

    monkeypatch.setattr("powercontext_eval.web.worker.load_report", lambda *args: object())
    assert EvaluationWorker(config, store, runner=runner, clock=lambda: NOW).run_once() is True
    failed = store.get(task.task_id)
    assert failed.status is TaskStatus.FAILED
    assert failed.failure_category is FailureCategory.REPORT_GENERATION
    assert failed.failure_summary == "Evaluation report validation failed."


def test_run_forever_recovers_once_and_waits_only_when_idle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = _store(config)
    waits = []
    worker: EvaluationWorker

    def wait(seconds: float) -> None:
        waits.append(seconds)
        worker.stop()

    worker = EvaluationWorker(
        config,
        store,
        runner=lambda *args, **kwargs: pytest.fail("runner should not run"),
        clock=lambda: NOW,
        sleep=wait,
    )
    recoveries = []
    original = store.recover_expired

    def recover(*, now: datetime) -> list[str]:
        recoveries.append(now)
        return original(now=now)

    monkeypatch.setattr(store, "recover_expired", recover)
    worker.run_forever()

    assert recoveries == [NOW]
    assert waits == [config.poll_seconds]
