import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from powercontext_eval.paths import EvaluationPaths
from powercontext_eval.web.batches import BatchControlEventType, BatchCreate, BatchStatus
from powercontext_eval.web.controls import BatchControlIntent, BatchPauseReason
from powercontext_eval.web.models import (
    FailureCategory,
    SafeFailure,
    TaskCreate,
    TaskPhase,
    TaskResult,
    TaskStatus,
)
from powercontext_eval.web.store import TaskConflict, TaskNotFound, TaskOwnershipError, TaskStore
from powercontext_eval.web.usage import UsageSnapshot

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


def batch_request(key: str) -> BatchCreate:
    return BatchCreate(
        powercontext_ref="latest",
        benchmark="swebench-pro",
        task_set="swebench-pro-public-v2",
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        treatment_mode="off_on",
        idempotency_key=key,
    )


def usage_snapshot(*, used_percent: int, observed_at: datetime = NOW) -> UsageSnapshot:
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


def test_initialize_migrates_legacy_task_without_deleting_it(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                queue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                version INTEGER NOT NULL DEFAULT 0,
                failure_category TEXT,
                failure_phase TEXT,
                failure_summary TEXT,
                result_json TEXT
            );
            CREATE TABLE worker_lease (
                singleton INTEGER PRIMARY KEY,
                worker_id TEXT NOT NULL,
                task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
                expires_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO tasks(task_id, idempotency_key, request_json, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("run-legacy", "legacy-key", request("legacy-key").model_dump_json(), "queued", NOW.isoformat()),
        )
    store = TaskStore(database, lease_duration=timedelta(seconds=60))

    store.initialize()

    legacy = store.get("run-legacy")
    assert legacy.batch_id is None
    assert legacy.source_index is None
    assert legacy.instance_id == request("legacy-key").instance_id
    assert store.list_batches() == []


def test_initialize_migrates_current_cancelled_batch_control_without_rewriting_children(database: Path) -> None:
    batch = batch_request("legacy-batch")
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE batches (
                batch_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_json TEXT NOT NULL,
                total_tasks INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                resolved_powercontext_sha TEXT
            );
            CREATE TABLE tasks (
                queue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_json TEXT NOT NULL,
                batch_id TEXT REFERENCES batches(batch_id),
                instance_id TEXT,
                source_index INTEGER,
                status TEXT NOT NULL,
                phase TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                version INTEGER NOT NULL DEFAULT 0,
                failure_category TEXT,
                failure_phase TEXT,
                failure_summary TEXT,
                result_json TEXT
            );
            CREATE TABLE worker_lease (
                singleton INTEGER PRIMARY KEY,
                worker_id TEXT NOT NULL,
                task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
                expires_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO batches(
                batch_id, idempotency_key, request_json, total_tasks, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("batch-legacy", batch.idempotency_key, batch.model_dump_json(), 2, NOW.isoformat()),
        )
        for index in range(2):
            child = request(f"legacy-child-{index}").model_copy(
                update={"instance_id": f"instance_owner__repo-{index}"}
            )
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, idempotency_key, request_json, batch_id, instance_id,
                    source_index, status, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"run-legacy-{index}",
                    child.idempotency_key,
                    child.model_dump_json(),
                    "batch-legacy",
                    child.instance_id,
                    index,
                    TaskStatus.CANCELLED.value,
                    NOW.isoformat(),
                    (NOW + timedelta(seconds=1)).isoformat(),
                ),
            )
    store = TaskStore(database, lease_duration=timedelta(seconds=60))

    store.initialize()
    store.initialize()

    migrated = store.get_batch("batch-legacy")
    assert migrated.status is BatchStatus.CANCELLED
    assert migrated.control.intent is BatchControlIntent.CANCEL
    assert migrated.control.usage_pause_percent == 80
    assert migrated.control.version == 0
    assert [task.status for task in store.list_batch_tasks("batch-legacy")] == [
        TaskStatus.CANCELLED,
        TaskStatus.CANCELLED,
    ]
    assert store.latest_usage_snapshot() is None
    assert store.list_control_events("batch-legacy") == ()


def test_usage_snapshots_are_append_only_and_survive_restart(database: Path) -> None:
    store = TaskStore(database, lease_duration=timedelta(seconds=60))
    store.initialize()
    first = usage_snapshot(used_percent=9)
    second = usage_snapshot(used_percent=12, observed_at=NOW + timedelta(minutes=1))

    assert store.save_usage_snapshot(first) == first
    assert store.save_usage_snapshot(second) == second
    assert store.latest_usage_snapshot() == second

    restarted = TaskStore(database, lease_duration=timedelta(seconds=60))
    restarted.initialize()
    assert restarted.latest_usage_snapshot() == second
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT snapshot_json FROM usage_snapshots ORDER BY snapshot_seq ASC"
        ).fetchall()
    assert [UsageSnapshot.model_validate_json(row[0], strict=True) for row in rows] == [first, second]


def test_create_batch_expands_every_instance_atomically_in_source_order(store: TaskStore, tmp_path: Path) -> None:
    instance_ids = (
        "instance_owner__repo-a",
        "instance_owner__repo-b",
        "instance_owner__repo-c",
    )

    batch, created = store.create_batch(batch_request("batch-key"), instance_ids, now=NOW)

    assert created is True
    assert batch.total_tasks == 3
    assert batch.status is BatchStatus.QUEUED
    children = store.list_batch_tasks(batch.batch_id)
    assert [task.instance_id for task in children] == list(instance_ids)
    assert [task.source_index for task in children] == [0, 1, 2]
    assert all(task.batch_id == batch.batch_id for task in children)
    assert all(EvaluationPaths(tmp_path, task.task_id) for task in children)
    assert store.health_snapshot(now=NOW)["queued_tasks"] == 3


def test_pause_without_a_running_task_is_immediate_idempotent_and_restart_safe(database: Path) -> None:
    store = TaskStore(database, lease_duration=timedelta(seconds=60))
    store.initialize()
    batch = store.create_batch(
        batch_request("pause-immediate"),
        ("instance_owner__repo-a", "instance_owner__repo-b"),
        now=NOW,
    )[0]

    paused = store.request_pause(batch.batch_id, reason=BatchPauseReason.USER, now=NOW + timedelta(seconds=1))
    replay = store.request_pause(batch.batch_id, reason=BatchPauseReason.USER, now=NOW + timedelta(seconds=2))

    assert paused.status is BatchStatus.PAUSED
    assert paused.control.intent is BatchControlIntent.PAUSE
    assert paused.control.pause_reason is BatchPauseReason.USER
    assert paused.control.version == 1
    assert replay == paused
    assert [task.status for task in store.list_batch_tasks(batch.batch_id)] == [
        TaskStatus.QUEUED,
        TaskStatus.QUEUED,
    ]
    assert [event.event_type for event in store.list_control_events(batch.batch_id)] == [
        BatchControlEventType.BATCH_CREATED,
        BatchControlEventType.PAUSE_REQUESTED,
        BatchControlEventType.PAUSED,
    ]

    restarted = TaskStore(database, lease_duration=timedelta(seconds=60))
    restarted.initialize()
    assert restarted.get_batch(batch.batch_id) == paused


def test_pause_and_cancel_wait_for_the_running_benchmark_task_boundary(store: TaskStore) -> None:
    paused_batch = store.create_batch(
        batch_request("pause-boundary"),
        ("instance_owner__repo-a", "instance_owner__repo-b"),
        now=NOW,
    )[0]
    running = store.claim_next("worker-a", now=NOW + timedelta(seconds=1))
    assert running is not None

    pausing = store.request_pause(
        paused_batch.batch_id,
        reason=BatchPauseReason.USER,
        now=NOW + timedelta(seconds=2),
    )

    assert pausing.status is BatchStatus.PAUSING
    assert [event.event_type for event in store.list_control_events(paused_batch.batch_id)][-1] is (
        BatchControlEventType.PAUSE_REQUESTED
    )
    store.succeed(
        running.task_id,
        "worker-a",
        TaskResult(
            artifact_dir="/safe/artifacts",
            report_path="/safe/artifacts/report.md",
            off_resolved=False,
            on_resolved=True,
        ),
        now=NOW + timedelta(seconds=3),
    )
    paused = store.finalize_batch_intent_after_attempt(paused_batch.batch_id, now=NOW + timedelta(seconds=3))

    assert paused.status is BatchStatus.PAUSED
    assert [task.status for task in store.list_batch_tasks(paused_batch.batch_id)] == [
        TaskStatus.SUCCEEDED,
        TaskStatus.QUEUED,
    ]
    assert [event.event_type for event in store.list_control_events(paused_batch.batch_id)][-1] is (
        BatchControlEventType.PAUSED
    )
    store.request_cancel(paused_batch.batch_id, now=NOW + timedelta(seconds=4))

    cancelled_batch = store.create_batch(
        batch_request("cancel-boundary"),
        ("instance_owner__repo-c", "instance_owner__repo-d"),
        now=NOW + timedelta(seconds=5),
    )[0]
    cancelled_running = store.claim_next("worker-a", now=NOW + timedelta(seconds=6))
    assert cancelled_running is not None
    assert cancelled_running.batch_id == cancelled_batch.batch_id

    cancelling = store.request_cancel(cancelled_batch.batch_id, now=NOW + timedelta(seconds=7))

    assert cancelling.status is BatchStatus.CANCELLING
    store.fail(
        cancelled_running.task_id,
        "worker-a",
        SafeFailure(
            category=FailureCategory.CODEX_EXECUTION,
            phase=TaskPhase.RUNNING_OFF,
            summary="Safe failure",
        ),
        now=NOW + timedelta(seconds=8),
    )
    cancelled = store.finalize_batch_intent_after_attempt(
        cancelled_batch.batch_id,
        now=NOW + timedelta(seconds=8),
    )

    assert cancelled.status is BatchStatus.CANCELLED
    assert [task.status for task in store.list_batch_tasks(cancelled_batch.batch_id)] == [
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ]
    assert [event.event_type for event in store.list_control_events(cancelled_batch.batch_id)][-2:] == [
        BatchControlEventType.CANCEL_REQUESTED,
        BatchControlEventType.CANCELLED,
    ]


def test_cancel_without_a_running_task_marks_queued_tasks_once(store: TaskStore) -> None:
    batch = store.create_batch(
        batch_request("cancel-immediate"),
        ("instance_owner__repo-a", "instance_owner__repo-b"),
        now=NOW,
    )[0]

    cancelled = store.request_cancel(batch.batch_id, now=NOW + timedelta(seconds=1))
    replay = store.request_cancel(batch.batch_id, now=NOW + timedelta(seconds=2))

    assert cancelled.status is BatchStatus.CANCELLED
    assert replay == cancelled
    assert [task.status for task in store.list_batch_tasks(batch.batch_id)] == [
        TaskStatus.CANCELLED,
        TaskStatus.CANCELLED,
    ]
    assert [event.event_type for event in store.list_control_events(batch.batch_id)] == [
        BatchControlEventType.BATCH_CREATED,
        BatchControlEventType.CANCEL_REQUESTED,
        BatchControlEventType.CANCELLED,
    ]


def test_resume_requires_usage_below_threshold_and_never_happens_implicitly(store: TaskStore) -> None:
    batch = store.create_batch(
        batch_request("resume-control"),
        ("instance_owner__repo-a",),
        now=NOW,
    )[0]
    paused = store.request_pause(batch.batch_id, reason=BatchPauseReason.USER, now=NOW + timedelta(seconds=1))
    at_threshold = usage_snapshot(used_percent=80, observed_at=NOW + timedelta(seconds=2))
    store.save_usage_snapshot(at_threshold)

    with pytest.raises(TaskConflict, match="threshold"):
        store.request_resume(batch.batch_id, snapshot=at_threshold, now=NOW + timedelta(seconds=2))

    assert store.get_batch(batch.batch_id) == paused
    below_threshold = usage_snapshot(used_percent=79, observed_at=NOW + timedelta(seconds=3))
    store.save_usage_snapshot(below_threshold)
    resumed = store.request_resume(
        batch.batch_id,
        snapshot=below_threshold,
        now=NOW + timedelta(seconds=3),
    )

    assert resumed.status is BatchStatus.QUEUED
    assert resumed.control.intent is BatchControlIntent.RUN
    assert resumed.control.pause_reason is None
    assert resumed.control.version == paused.control.version + 1
    assert [event.event_type for event in store.list_control_events(batch.batch_id)][-2:] == [
        BatchControlEventType.RESUME_REQUESTED,
        BatchControlEventType.RESUMED,
    ]


def test_threshold_updates_use_optimistic_concurrency_and_do_not_auto_resume(store: TaskStore) -> None:
    batch = store.create_batch(
        batch_request("threshold-control"),
        ("instance_owner__repo-a",),
        now=NOW,
    )[0]
    paused = store.request_pause(
        batch.batch_id,
        reason=BatchPauseReason.USAGE_THRESHOLD,
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(TaskConflict, match="version"):
        store.update_usage_threshold(
            batch.batch_id,
            percent=90,
            expected_version=0,
            now=NOW + timedelta(seconds=2),
        )

    updated = store.update_usage_threshold(
        batch.batch_id,
        percent=90,
        expected_version=paused.control.version,
        now=NOW + timedelta(seconds=2),
    )
    replay = store.update_usage_threshold(
        batch.batch_id,
        percent=90,
        expected_version=updated.control.version,
        now=NOW + timedelta(seconds=3),
    )

    assert updated.status is BatchStatus.PAUSED
    assert updated.control.intent is BatchControlIntent.PAUSE
    assert updated.control.usage_pause_percent == 90
    assert updated.control.version == paused.control.version + 1
    assert replay == updated
    threshold_events = [
        event
        for event in store.list_control_events(batch.batch_id)
        if event.event_type is BatchControlEventType.THRESHOLD_CHANGED
    ]
    assert len(threshold_events) == 1
    assert threshold_events[0].details == {"from_percent": 80, "to_percent": 90}


def test_create_batch_replays_idempotency_key_without_duplicate_children(store: TaskStore) -> None:
    instance_ids = ("instance_owner__repo-a", "instance_owner__repo-b")
    original, original_created = store.create_batch(batch_request("batch-replay"), instance_ids, now=NOW)

    replay, replay_created = store.create_batch(
        batch_request("batch-replay"),
        ("instance_other__repo-x",),
        now=NOW + timedelta(seconds=1),
    )

    assert original_created is True
    assert replay_created is False
    assert replay == original
    assert [task.instance_id for task in store.list_batch_tasks(replay.batch_id)] == list(instance_ids)


def test_create_batch_failure_leaves_neither_batch_nor_children(store: TaskStore) -> None:
    with pytest.raises(ValueError):
        store.create_batch(
            batch_request("batch-rollback"),
            ("instance_owner__repo-a", "unsafe/instance"),
            now=NOW,
        )

    assert store.list_batches() == []
    assert store.list_tasks(status=None, limit=10, offset=0) == []


def test_create_batch_rejects_duplicate_instance_ids(store: TaskStore) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        store.create_batch(
            batch_request("batch-duplicates"),
            ("instance_owner__repo-a", "instance_owner__repo-a"),
            now=NOW,
        )


def test_batch_revision_pin_is_idempotent_and_rejects_conflict(store: TaskStore) -> None:
    batch = store.create_batch(
        batch_request("batch-pin"),
        ("instance_owner__repo-a",),
        now=NOW,
    )[0]

    pinned = store.pin_batch_revision(batch.batch_id, "a" * 40)
    replay = store.pin_batch_revision(batch.batch_id, "a" * 40)

    assert pinned.resolved_powercontext_sha == "a" * 40
    assert replay == pinned
    with pytest.raises(TaskConflict, match="different"):
        store.pin_batch_revision(batch.batch_id, "b" * 40)


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
