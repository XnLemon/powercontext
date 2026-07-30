from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from powercontext_eval.benchmarks.base import GoldCheckFailed
from powercontext_eval.benchmarks.swebench_pro.adapter import DatasetSchemaError
from powercontext_eval.benchmarks.swebench_pro.evaluator import OfficialResultError
from powercontext_eval.codex import CodexInfrastructureError
from powercontext_eval.errors import GitSourceError
from powercontext_eval.powercontext_sut import InvalidTreatment, UnsafeSutConfiguration
from powercontext_eval.runner import MinimalRunResult, RunPhase
from powercontext_eval.web.batches import BatchCreate, BatchStatus
from powercontext_eval.web.config import WebConfig
from powercontext_eval.web.controls import BatchControlIntent, BatchPauseReason
from powercontext_eval.web.models import FailureCategory, SafeFailure, TaskCreate, TaskPhase, TaskStatus
from powercontext_eval.web.store import TaskStore
from powercontext_eval.web.usage import CodexUsageProbe, UsageSnapshot, UsageUnavailable
from powercontext_eval.web.worker import EvaluationWorker

NOW = datetime(2026, 7, 29, 1, 2, 3, tzinfo=UTC)


def _usage(used_percent: int, *, observed_at: datetime = NOW) -> UsageSnapshot:
    return UsageSnapshot(
        limit_id="codex",
        used_percent=used_percent,
        remaining_percent=100 - used_percent,
        window_duration_minutes=10_080,
        resets_at=NOW + timedelta(days=7),
        observed_at=observed_at,
        plan_type="pro",
        account_tokens=1_234,
    )


class FakeUsageProbe:
    def __init__(self, observations: list[UsageSnapshot | Exception]) -> None:
        self.observations = observations
        self.calls: list[datetime] = []

    def read(self, *, now: datetime) -> UsageSnapshot:
        self.calls.append(now)
        observation = self.observations.pop(0)
        if isinstance(observation, Exception):
            raise observation
        return observation.model_copy(update={"observed_at": now})


@pytest.fixture(autouse=True)
def _default_safe_usage_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        CodexUsageProbe,
        "read",
        lambda _self, *, now: _usage(9, observed_at=now),
    )


def _config(
    root: Path,
    *,
    lease_seconds: int = 2,
    poll_seconds: float = 0.01,
    usage_probe_seconds: int = 60,
) -> WebConfig:
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
        usage_probe_seconds=usage_probe_seconds,
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


def _create_batch(
    store: TaskStore,
    *,
    key: str = "batch-worker-test",
    instance_ids: tuple[str, ...] = ("instance_owner__repo-a", "instance_owner__repo-b"),
) -> Any:
    return store.create_batch(
        BatchCreate(
            powercontext_ref="latest",
            benchmark="swebench-pro",
            task_set="swebench-pro-public-v2",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            treatment_mode="off_on",
            idempotency_key=key,
        ),
        instance_ids,
        now=NOW,
    )[0]


class FakeCatalog:
    def __init__(self, instance_ids: tuple[str, ...]) -> None:
        self.instances = {instance_id: object() for instance_id in instance_ids}

    def require(self, instance_id: str) -> object:
        return self.instances[instance_id]


class FakeSource:
    def __init__(self, sha: str = "c" * 40) -> None:
        self.sha = sha
        self.resolve_calls: list[tuple[object, object]] = []

    def resolve(self, source: object, requested: object) -> object:
        self.resolve_calls.append((source, requested))
        return SimpleNamespace(sha=self.sha)


def _successful_batch_runner(
    config: WebConfig,
    calls: list[tuple[object, object]],
) -> Any:
    def runner(run_config: Any, *, instance: object, on_phase: Any) -> MinimalRunResult:
        calls.append((run_config, instance))
        run_dir = config.run_root / "runs" / run_config.run_id
        run_dir.mkdir(parents=True)
        report_path = run_dir / "report.md"
        report_path.write_text("safe")
        return MinimalRunResult(run_config.run_id, report_path, True, False)

    return runner


def test_worker_pauses_before_claim_when_usage_reaches_configured_threshold(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    batch = _create_batch(store, instance_ids=("instance_owner__repo-a",))
    calls: list[str] = []
    worker = EvaluationWorker(
        config,
        store,
        usage_probe=FakeUsageProbe([_usage(80)]),
        runner=lambda run_config, **_kwargs: calls.append(run_config.run_id),
        source=FakeSource(),
        catalog=FakeCatalog(("instance_owner__repo-a",)),
        clock=lambda: NOW,
    )

    assert worker.run_once() is False
    assert calls == []
    paused = store.get_batch(batch.batch_id)
    assert paused.status is BatchStatus.PAUSED
    assert paused.control.intent is BatchControlIntent.PAUSE
    assert paused.control.pause_reason is BatchPauseReason.USAGE_THRESHOLD
    assert store.latest_usage_snapshot() == _usage(80)


def test_worker_reuses_usage_until_the_probe_interval_expires(tmp_path: Path) -> None:
    config = _config(tmp_path, usage_probe_seconds=60)
    store = _store(config)
    store.save_usage_snapshot(_usage(10, observed_at=NOW))
    probe = FakeUsageProbe([_usage(11), _usage(12)])
    observations = iter((NOW + timedelta(seconds=30), NOW + timedelta(seconds=61)))
    worker = EvaluationWorker(
        config,
        store,
        usage_probe=probe,
        clock=lambda: next(observations),
    )

    assert worker.run_once() is False
    assert probe.calls == []

    assert worker.run_once() is False
    assert probe.calls == [NOW + timedelta(seconds=61)]
    assert store.latest_usage_snapshot() == _usage(11, observed_at=NOW + timedelta(seconds=61))


def test_worker_finishes_current_task_before_honoring_user_pause(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = _store(config)
    batch = _create_batch(store)
    calls: list[str] = []

    def runner(run_config: Any, *, instance: object, on_phase: Any) -> MinimalRunResult:
        del instance, on_phase
        calls.append(run_config.run_id)
        store.request_pause(batch.batch_id, reason=BatchPauseReason.USER, now=NOW)
        report = config.run_root / "runs" / run_config.run_id / "report.md"
        report.parent.mkdir(parents=True)
        report.write_text("safe")
        return MinimalRunResult(run_config.run_id, report, True, True)

    monkeypatch.setattr("powercontext_eval.web.worker.load_report", lambda *args: object())
    worker = EvaluationWorker(
        config,
        store,
        usage_probe=FakeUsageProbe([_usage(9), _usage(10)]),
        runner=runner,
        source=FakeSource(),
        catalog=FakeCatalog(("instance_owner__repo-a", "instance_owner__repo-b")),
        clock=lambda: NOW,
    )

    assert worker.run_once() is True

    assert len(calls) == 1
    assert [task.status for task in store.list_batch_tasks(batch.batch_id)] == [
        TaskStatus.SUCCEEDED,
        TaskStatus.QUEUED,
    ]
    assert store.get_batch(batch.batch_id).status is BatchStatus.PAUSED


def test_worker_finishes_current_task_before_cancelling_remaining_tasks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = _store(config)
    batch = _create_batch(store)

    def runner(run_config: Any, *, instance: object, on_phase: Any) -> MinimalRunResult:
        del instance, on_phase
        store.request_cancel(batch.batch_id, now=NOW)
        report = config.run_root / "runs" / run_config.run_id / "report.md"
        report.parent.mkdir(parents=True)
        report.write_text("safe")
        return MinimalRunResult(run_config.run_id, report, False, False)

    monkeypatch.setattr("powercontext_eval.web.worker.load_report", lambda *args: object())
    worker = EvaluationWorker(
        config,
        store,
        usage_probe=FakeUsageProbe([_usage(9), _usage(10)]),
        runner=runner,
        source=FakeSource(),
        catalog=FakeCatalog(("instance_owner__repo-a", "instance_owner__repo-b")),
        clock=lambda: NOW,
    )

    assert worker.run_once() is True

    assert [task.status for task in store.list_batch_tasks(batch.batch_id)] == [
        TaskStatus.SUCCEEDED,
        TaskStatus.CANCELLED,
    ]
    assert store.get_batch(batch.batch_id).status is BatchStatus.CANCELLED


def test_worker_skips_paused_oldest_batch_and_claims_next_runnable_batch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    oldest = _create_batch(
        store,
        key="batch-paused-oldest",
        instance_ids=("instance_owner__repo-a",),
    )
    runnable = _create_batch(
        store,
        key="batch-runnable-next",
        instance_ids=("instance_owner__repo-b",),
    )
    store.request_pause(oldest.batch_id, reason=BatchPauseReason.USER, now=NOW)
    calls: list[str] = []

    def runner(run_config: Any, *, instance: object, on_phase: Any) -> MinimalRunResult:
        del instance, on_phase
        calls.append(run_config.run_id)
        raise RuntimeError("stop after claim")

    worker = EvaluationWorker(
        config,
        store,
        usage_probe=FakeUsageProbe([_usage(9), _usage(10)]),
        runner=runner,
        source=FakeSource(),
        catalog=FakeCatalog(("instance_owner__repo-a", "instance_owner__repo-b")),
        clock=lambda: NOW,
    )

    assert worker.run_once() is True
    assert calls == [store.list_batch_tasks(runnable.batch_id)[0].task_id]
    assert store.list_batch_tasks(oldest.batch_id)[0].status is TaskStatus.QUEUED


def test_worker_fails_closed_when_usage_is_unavailable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    batch = _create_batch(store, instance_ids=("instance_owner__repo-a",))
    calls: list[str] = []
    worker = EvaluationWorker(
        config,
        store,
        usage_probe=FakeUsageProbe([UsageUnavailable("private failure")]),
        runner=lambda run_config, **_kwargs: calls.append(run_config.run_id),
        source=FakeSource(),
        catalog=FakeCatalog(("instance_owner__repo-a",)),
        clock=lambda: NOW,
    )

    assert worker.run_once() is False

    assert calls == []
    paused = store.get_batch(batch.batch_id)
    assert paused.status is BatchStatus.PAUSED
    assert paused.control.pause_reason is BatchPauseReason.USAGE_UNAVAILABLE


def test_worker_executes_only_the_new_attempt_when_a_failed_task_is_retried(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    batch = _create_batch(store, instance_ids=("instance_owner__repo-a",))
    task = store.list_batch_tasks(batch.batch_id)[0]
    claimed = store.claim_next("setup-worker", now=NOW)
    assert claimed is not None
    store.fail(
        task.task_id,
        "setup-worker",
        SafeFailure(
            category=FailureCategory.CODEX_EXECUTION,
            phase=TaskPhase.RUNNING_OFF,
            summary="First attempt failed",
        ),
        now=NOW,
    )
    retry, created = store.retry_failed_task(
        batch.batch_id,
        task.task_id,
        idempotency_key="retry-worker-0001",
        now=NOW,
    )
    assert created is True
    calls: list[str] = []

    def runner(run_config: Any, *, instance: object, on_phase: Any) -> MinimalRunResult:
        del instance, on_phase
        calls.append(run_config.run_id)
        raise CodexInfrastructureError("retry failed")

    worker = EvaluationWorker(
        config,
        store,
        usage_probe=FakeUsageProbe([_usage(9), _usage(10)]),
        runner=runner,
        source=FakeSource(),
        catalog=FakeCatalog(("instance_owner__repo-a",)),
        clock=lambda: NOW,
    )

    assert worker.run_once() is True

    assert retry.attempt_id == f"{task.task_id}.attempt-0002"
    assert calls == [f"{task.task_id}-attempt-0002"]
    attempts = store.list_task_attempts(batch.batch_id, task.task_id)
    assert [attempt.status for attempt in attempts] == [TaskStatus.FAILED, TaskStatus.FAILED]
    assert attempts[0].failure_summary == "First attempt failed"


def test_latest_is_pinned_once_and_every_child_uses_catalog_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    store = _store(config)
    instance_ids = ("instance_owner__repo-a", "instance_owner__repo-b")
    batch = _create_batch(store, instance_ids=instance_ids)
    source = FakeSource()
    catalog = FakeCatalog(instance_ids)
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr("powercontext_eval.web.worker.load_report", lambda *args: object())
    worker = EvaluationWorker(
        config,
        store,
        runner=_successful_batch_runner(config, calls),
        source=source,
        catalog=catalog,
        clock=lambda: NOW,
    )

    assert worker.run_once() is True
    assert worker.run_once() is True

    assert len(source.resolve_calls) == 1
    assert [call[0].powercontext_ref for call in calls] == ["commit:" + source.sha, "commit:" + source.sha]
    assert [call[1] for call in calls] == [catalog.instances[instance_id] for instance_id in instance_ids]
    persisted = store.get_batch(batch.batch_id)
    assert persisted.resolved_powercontext_sha == source.sha
    assert persisted.status is BatchStatus.COMPLETED


def test_only_one_child_runs_physically_across_multiple_batches(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    first_batch = _create_batch(
        store,
        key="batch-global-first",
        instance_ids=("instance_owner__repo-a",),
    )
    _create_batch(
        store,
        key="batch-global-second",
        instance_ids=("instance_owner__repo-b",),
    )
    catalog = FakeCatalog(("instance_owner__repo-a", "instance_owner__repo-b"))
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def runner(run_config: Any, *, instance: object, on_phase: Any) -> MinimalRunResult:
        calls.append(run_config.run_id)
        entered.set()
        release.wait(timeout=2)
        raise RuntimeError("stop")

    first = EvaluationWorker(
        config,
        store,
        runner=runner,
        source=FakeSource(),
        catalog=catalog,
        worker_id="first",
    )
    second = EvaluationWorker(
        config,
        store,
        runner=runner,
        source=FakeSource(),
        catalog=catalog,
        worker_id="second",
    )
    thread = threading.Thread(target=first.run_once)
    thread.start()
    assert entered.wait(timeout=2)

    assert second.run_once() is False

    release.set()
    thread.join(timeout=2)
    assert calls == [store.list_batch_tasks(first_batch.batch_id)[0].task_id]


def test_failed_batch_child_does_not_prevent_later_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    instance_ids = ("instance_owner__repo-a", "instance_owner__repo-b")
    batch = _create_batch(store, instance_ids=instance_ids)
    catalog = FakeCatalog(instance_ids)
    calls: list[str] = []

    def runner(run_config: Any, *, instance: object, on_phase: Any) -> MinimalRunResult:
        calls.append(run_config.run_id)
        if len(calls) == 1:
            raise CodexInfrastructureError("first child failed")
        run_dir = config.run_root / "runs" / run_config.run_id
        run_dir.mkdir(parents=True)
        report = run_dir / "report.md"
        report.write_text("safe")
        return MinimalRunResult(run_config.run_id, report, True, True)

    monkeypatch.setattr("powercontext_eval.web.worker.load_report", lambda *args: object())
    worker = EvaluationWorker(
        config,
        store,
        runner=runner,
        source=FakeSource(),
        catalog=catalog,
        clock=lambda: NOW,
    )

    assert worker.run_once() is True
    assert worker.run_once() is True

    children = store.list_batch_tasks(batch.batch_id)
    assert [child.status for child in children] == [TaskStatus.FAILED, TaskStatus.SUCCEEDED]
    assert store.get_batch(batch.batch_id).status is BatchStatus.COMPLETED


def test_restart_reuses_persisted_batch_sha_and_completed_children(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    store = _store(config)
    instance_ids = ("instance_owner__repo-a", "instance_owner__repo-b")
    batch = _create_batch(store, instance_ids=instance_ids)
    catalog = FakeCatalog(instance_ids)
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr("powercontext_eval.web.worker.load_report", lambda *args: object())
    first_source = FakeSource()
    first = EvaluationWorker(
        config,
        store,
        runner=_successful_batch_runner(config, calls),
        source=first_source,
        catalog=catalog,
        clock=lambda: NOW,
    )
    assert first.run_once() is True

    class UnexpectedSource(FakeSource):
        def resolve(self, source: object, requested: object) -> object:
            pytest.fail("persisted batch SHA should avoid resolving latest after restart")

    restarted = EvaluationWorker(
        config,
        store,
        runner=_successful_batch_runner(config, calls),
        source=UnexpectedSource(),
        catalog=catalog,
        clock=lambda: NOW,
    )
    assert restarted.run_once() is True

    children = store.list_batch_tasks(batch.batch_id)
    assert [child.status for child in children] == [TaskStatus.SUCCEEDED, TaskStatus.SUCCEEDED]
    assert len(first_source.resolve_calls) == 1


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
