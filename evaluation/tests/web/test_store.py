from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from powercontext_eval.paths import EvaluationPaths
from powercontext_eval.web.models import (
    FailureCategory,
    SafeFailure,
    TaskCreate,
    TaskPhase,
    TaskResult,
    TaskStatus,
)
from powercontext_eval.web.store import TaskConflict, TaskNotFound, TaskOwnershipError, TaskStore

NOW = datetime(2026, 7, 29, 1, 2, 3, tzinfo=UTC)


def request(key: str) -> TaskCreate:
    return TaskCreate(
        powercontext_ref="commit:" + "a" * 40,
        benchmark="swebench-pro",
        instance_id="instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9",
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        treatment_mode="off_on",
        idempotency_key=key,
    )


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "tasks.sqlite3"


@pytest.fixture
def store(database: Path) -> TaskStore:
    task_store = TaskStore(database, lease_duration=timedelta(seconds=60))
    task_store.initialize()
    return task_store


def test_initialize_is_idempotent_and_creates_expected_schema(database: Path) -> None:
    store = TaskStore(database, lease_duration=timedelta(seconds=60))

    store.initialize()
    store.initialize()

    assert store.list_tasks(status=None, limit=10, offset=0) == []


def test_create_replays_idempotency_key_without_reordering(store: TaskStore) -> None:
    original, original_created = store.create(request("same-key"), now=NOW)
    later, _ = store.create(request("later-key"), now=NOW + timedelta(seconds=1))

    replay, replay_created = store.create(request("same-key"), now=NOW + timedelta(seconds=2))

    assert original_created is True
    assert replay_created is False
    assert replay == original
    assert [item.task_id for item in store.list_tasks(status=None, limit=10, offset=0)] == [
        original.task_id,
        later.task_id,
    ]


def test_distinct_idempotency_keys_create_distinct_safe_sortable_ids(store: TaskStore, tmp_path: Path) -> None:
    first, _ = store.create(request("distinct-1"), now=NOW)
    second, _ = store.create(request("distinct-2"), now=NOW)

    EvaluationPaths(tmp_path, first.task_id)
    EvaluationPaths(tmp_path, second.task_id)
    assert first.task_id != second.task_id
    assert first.task_id < second.task_id


def test_fifo_order_stable_pagination_and_status_filtering(store: TaskStore) -> None:
    first, _ = store.create(request("fifo-key-1"), now=NOW + timedelta(seconds=2))
    second, _ = store.create(request("fifo-key-2"), now=NOW)
    third, _ = store.create(request("fifo-key-3"), now=NOW)
    store.cancel_queued(second.task_id, now=NOW + timedelta(seconds=3))

    page = store.list_tasks(status=None, limit=2, offset=1)

    assert [item.task_id for item in page] == [second.task_id, third.task_id]
    assert [item.task_id for item in store.list_tasks(status=TaskStatus.QUEUED, limit=10, offset=0)] == [
        first.task_id,
        third.task_id,
    ]
    assert store.list_tasks(status=TaskStatus.CANCELLED, limit=10, offset=0)[0].task_id == second.task_id


def test_newest_order_is_applied_before_stable_pagination(store: TaskStore) -> None:
    created = [store.create(request(f"newest-key-{index:02d}"), now=NOW)[0] for index in range(55)]

    newest_page = store.list_tasks(status=None, order="newest", limit=50, offset=0)
    oldest_page = store.list_tasks(status=None, limit=50, offset=0)

    assert [item.task_id for item in newest_page] == [item.task_id for item in reversed(created[-50:])]
    assert [item.task_id for item in oldest_page] == [item.task_id for item in created[:50]]


def test_get_returns_record_and_unknown_task_raises(store: TaskStore) -> None:
    created, _ = store.create(request("lookup-key"), now=NOW)

    assert store.get(created.task_id) == created
    with pytest.raises(TaskNotFound):
        store.get("missing-task")


def test_cancel_only_queued_task_and_increments_version(store: TaskStore) -> None:
    queued, _ = store.create(request("cancel-key"), now=NOW)

    cancelled = store.cancel_queued(queued.task_id, now=NOW + timedelta(seconds=1))

    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.finished_at == NOW + timedelta(seconds=1)
    assert cancelled.version == queued.version + 1
    with pytest.raises(TaskConflict):
        store.cancel_queued(cancelled.task_id, now=NOW + timedelta(seconds=2))


def test_queue_position_and_read_only_health_snapshot(store: TaskStore) -> None:
    first, _ = store.create(request("position-key-1"), now=NOW)
    second, _ = store.create(request("position-key-2"), now=NOW)
    store.cancel_queued(first.task_id, now=NOW + timedelta(seconds=1))

    assert store.queue_position(first.task_id) is None
    assert store.queue_position(second.task_id) == 1
    assert store.health_snapshot(now=NOW + timedelta(seconds=1)) == {
        "worker_lease_active": False,
        "queued_tasks": 1,
        "running_tasks": 0,
    }


def test_health_observes_only_nonexpired_worker_lease(store: TaskStore) -> None:
    task, _ = store.create(request("lease-health-key"), now=NOW)
    store.claim_next("worker", now=NOW)

    assert store.health_snapshot(now=NOW + timedelta(seconds=30))["worker_lease_active"] is True
    assert store.health_snapshot(now=NOW + timedelta(seconds=61))["worker_lease_active"] is False
    assert store.get(task.task_id).status is TaskStatus.RUNNING


def test_claim_is_fifo_atomic_and_globally_excludes_other_connection(database: Path) -> None:
    first_store = TaskStore(database, lease_duration=timedelta(seconds=60))
    second_store = TaskStore(database, lease_duration=timedelta(seconds=60))
    first_store.initialize()
    first, _ = first_store.create(request("claim-key-1"), now=NOW)
    second, _ = first_store.create(request("claim-key-2"), now=NOW)

    claimed = first_store.claim_next("worker-a", now=NOW + timedelta(seconds=1))

    assert claimed is not None
    assert claimed.task_id == first.task_id
    assert claimed.status is TaskStatus.RUNNING
    assert claimed.started_at == NOW + timedelta(seconds=1)
    assert claimed.version == first.version + 1
    assert second_store.claim_next("worker-b", now=NOW + timedelta(seconds=2)) is None
    assert second_store.list_tasks(status=TaskStatus.QUEUED, limit=10, offset=0)[0].task_id == second.task_id


def test_claim_clamps_stale_worker_time_to_task_creation_and_lease_chronology(store: TaskStore) -> None:
    created_at = NOW + timedelta(seconds=10)
    stale_worker_now = NOW
    queued, _ = store.create(request("stale-claim-clock"), now=created_at)

    claimed = store.claim_next("worker-a", now=stale_worker_now)

    assert claimed is not None
    assert claimed.task_id == queued.task_id
    assert claimed.started_at == created_at
    phased = store.set_phase(queued.task_id, "worker-a", TaskPhase.PREPARING, now=stale_worker_now)
    heartbeat = store.heartbeat(queued.task_id, "worker-a", now=stale_worker_now)
    assert phased.started_at == heartbeat.started_at == created_at
    assert store.health_snapshot(now=created_at + timedelta(seconds=59))["worker_lease_active"] is True
    assert store.health_snapshot(now=created_at + timedelta(seconds=61))["worker_lease_active"] is False


def test_heartbeat_requires_owner_and_increments_version(store: TaskStore) -> None:
    queued, _ = store.create(request("heartbeat-key"), now=NOW)
    running = store.claim_next("worker-a", now=NOW + timedelta(seconds=1))
    assert running is not None

    heartbeat = store.heartbeat(queued.task_id, "worker-a", now=NOW + timedelta(seconds=2))

    assert heartbeat.version == running.version + 1
    with pytest.raises(TaskOwnershipError):
        store.heartbeat(queued.task_id, "worker-b", now=NOW + timedelta(seconds=3))


def test_stale_heartbeat_cannot_shorten_an_existing_lease(store: TaskStore) -> None:
    queued, _ = store.create(request("monotonic-heartbeat"), now=NOW)
    running = store.claim_next("worker-a", now=NOW)
    assert running is not None

    renewed = store.heartbeat(queued.task_id, "worker-a", now=NOW + timedelta(seconds=30))
    stale = store.heartbeat(queued.task_id, "worker-a", now=NOW + timedelta(seconds=10))

    assert stale.version == renewed.version + 1
    assert store.health_snapshot(now=NOW + timedelta(seconds=75))["worker_lease_active"] is True
    assert store.health_snapshot(now=NOW + timedelta(seconds=91))["worker_lease_active"] is False


def test_phase_success_and_lease_release(store: TaskStore) -> None:
    queued, _ = store.create(request("success-key"), now=NOW)
    next_queued, _ = store.create(request("success-key-2"), now=NOW)
    running = store.claim_next("worker-a", now=NOW + timedelta(seconds=1))
    assert running is not None

    phased = store.set_phase(queued.task_id, "worker-a", TaskPhase.RUNNING_OFF, now=NOW + timedelta(seconds=2))
    result = TaskResult(
        artifact_dir="/safe/artifacts",
        report_path="/safe/artifacts/report.md",
        off_resolved=False,
        on_resolved=True,
    )
    succeeded = store.succeed(queued.task_id, "worker-a", result, now=NOW + timedelta(seconds=3))

    assert phased.phase is TaskPhase.RUNNING_OFF
    assert phased.version == running.version + 1
    assert succeeded.status is TaskStatus.SUCCEEDED
    assert succeeded.result == result
    assert succeeded.finished_at == NOW + timedelta(seconds=3)
    assert succeeded.version == phased.version + 1
    next_claimed = store.claim_next("worker-b", now=NOW + timedelta(seconds=4))
    assert next_claimed is not None and next_claimed.task_id == next_queued.task_id


def test_failure_records_safe_failure(store: TaskStore) -> None:
    queued, _ = store.create(request("failure-key"), now=NOW)
    running = store.claim_next("worker-a", now=NOW + timedelta(seconds=1))
    assert running is not None
    failure = SafeFailure(
        category=FailureCategory.CODEX_EXECUTION,
        phase=TaskPhase.RUNNING_ON,
        summary="Codex execution did not complete",
    )

    failed = store.fail(queued.task_id, "worker-a", failure, now=NOW + timedelta(seconds=2))

    assert failed.status is TaskStatus.FAILED
    assert failed.failure_category is FailureCategory.CODEX_EXECUTION
    assert failed.failure_phase is TaskPhase.RUNNING_ON
    assert failed.failure_summary == failure.summary
    assert failed.version == running.version + 1


def test_terminal_records_are_immutable(store: TaskStore) -> None:
    queued, _ = store.create(request("terminal-key"), now=NOW)
    store.cancel_queued(queued.task_id, now=NOW + timedelta(seconds=1))

    with pytest.raises(TaskConflict):
        store.set_phase(queued.task_id, "worker-a", TaskPhase.PREPARING, now=NOW + timedelta(seconds=2))


def test_expired_lease_recovery_interrupts_running_only(store: TaskStore) -> None:
    first, _ = store.create(request("recover-key-1"), now=NOW)
    second, _ = store.create(request("recover-key-2"), now=NOW)
    store.claim_next("worker-a", now=NOW + timedelta(seconds=1))

    recovered = store.recover_expired(now=NOW + timedelta(seconds=62))

    interrupted = store.get(first.task_id)
    assert recovered == [first.task_id]
    assert interrupted.status is TaskStatus.INTERRUPTED
    assert interrupted.failure_category is FailureCategory.WORKER_INTERRUPTION
    assert interrupted.failure_summary == "Evaluation worker lease expired"
    assert interrupted.version == 2
    assert store.get(second.task_id).status is TaskStatus.QUEUED
    claimed = store.claim_next("worker-b", now=NOW + timedelta(seconds=63))
    assert claimed is not None and claimed.task_id == second.task_id


def test_recover_unexpired_lease_is_noop(store: TaskStore) -> None:
    queued, _ = store.create(request("unexpired-key"), now=NOW)
    store.claim_next("worker-a", now=NOW + timedelta(seconds=1))

    assert store.recover_expired(now=NOW + timedelta(seconds=60)) == []
    assert store.get(queued.task_id).status is TaskStatus.RUNNING
