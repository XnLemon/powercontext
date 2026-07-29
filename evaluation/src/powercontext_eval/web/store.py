"""Durable SQLite-backed FIFO task queue for the evaluation console."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from re import fullmatch
from typing import Any, Literal, TypedDict

from powercontext_eval.paths import EvaluationPaths
from powercontext_eval.web.batches import (
    BatchControlEvent,
    BatchControlEventType,
    BatchCreate,
    BatchRecord,
    BatchStatus,
)
from powercontext_eval.web.controls import (
    BatchControlIntent,
    BatchControlState,
    BatchPauseReason,
    derive_controlled_batch_status,
)
from powercontext_eval.web.models import (
    RETRYABLE_FAILURES,
    FailureCategory,
    SafeFailure,
    TaskAttemptRecord,
    TaskCreate,
    TaskPhase,
    TaskRecord,
    TaskResult,
    TaskStatus,
    TaskSummary,
)
from powercontext_eval.web.usage import UsageSnapshot


class TaskStoreError(RuntimeError):
    """Base class for task-store domain failures."""


class TaskNotFound(TaskStoreError):
    """The requested task does not exist."""


class BatchNotFound(TaskStoreError):
    """The requested batch does not exist."""


class TaskConflict(TaskStoreError):
    """The requested transition conflicts with the task lifecycle."""


class TaskOwnershipError(TaskStoreError):
    """The worker does not own the active task lease."""


class HealthSnapshot(TypedDict):
    worker_lease_active: bool
    queued_tasks: int
    running_tasks: int


class TaskStore:
    """Persist tasks and coordinate a single global worker through SQLite."""

    def __init__(self, database: Path, *, lease_duration: timedelta) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._database = database
        self._lease_duration = lease_duration

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        """Create the queue schema and indexes if they do not exist."""
        self._database.parent.mkdir(parents=True, exist_ok=True)
        with self._write() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    batch_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_json TEXT NOT NULL,
                    total_tasks INTEGER NOT NULL CHECK (total_tasks > 0),
                    created_at TEXT NOT NULL,
                    resolved_powercontext_sha TEXT,
                    control_intent TEXT NOT NULL DEFAULT 'run',
                    usage_pause_percent INTEGER NOT NULL DEFAULT 80,
                    pause_reason TEXT,
                    control_updated_at TEXT,
                    control_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tasks (
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
                    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
                    failure_category TEXT,
                    failure_phase TEXT,
                    failure_summary TEXT,
                    result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS task_attempts (
                    attempt_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    phase TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
                    failure_category TEXT,
                    failure_phase TEXT,
                    failure_summary TEXT,
                    result_json TEXT,
                    UNIQUE(task_id, attempt_number)
                );
                CREATE TABLE IF NOT EXISTS worker_lease (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    worker_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL UNIQUE REFERENCES task_attempts(attempt_id),
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_snapshots (
                    snapshot_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_control_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )
            task_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
            if "batch_id" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN batch_id TEXT REFERENCES batches(batch_id)")
            if "instance_id" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN instance_id TEXT")
            if "source_index" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN source_index INTEGER")
            batch_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(batches)").fetchall()}
            if "control_intent" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN control_intent TEXT NOT NULL DEFAULT 'run'")
            if "usage_pause_percent" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN usage_pause_percent INTEGER NOT NULL DEFAULT 80")
            if "pause_reason" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN pause_reason TEXT")
            if "control_updated_at" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN control_updated_at TEXT")
            if "control_version" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN control_version INTEGER NOT NULL DEFAULT 0")
            connection.execute("UPDATE batches SET control_updated_at = created_at WHERE control_updated_at IS NULL")
            connection.execute(
                """
                UPDATE batches
                SET control_intent = ?
                WHERE EXISTS (
                    SELECT 1 FROM tasks WHERE tasks.batch_id = batches.batch_id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM tasks
                    WHERE tasks.batch_id = batches.batch_id
                    AND tasks.status != ?
                )
                """,
                (BatchControlIntent.CANCEL.value, TaskStatus.CANCELLED.value),
            )
            connection.execute(
                """
                INSERT INTO task_attempts(
                    attempt_id, task_id, attempt_number, idempotency_key, status,
                    phase, created_at, started_at, finished_at, version,
                    failure_category, failure_phase, failure_summary, result_json
                )
                SELECT
                    task_id || '.attempt-0001',
                    task_id,
                    1,
                    task_id || '.attempt-0001',
                    status,
                    phase,
                    created_at,
                    started_at,
                    finished_at,
                    version,
                    failure_category,
                    failure_phase,
                    failure_summary,
                    result_json
                FROM tasks
                WHERE NOT EXISTS (
                    SELECT 1 FROM task_attempts WHERE task_attempts.task_id = tasks.task_id
                )
                """
            )
            lease_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(worker_lease)").fetchall()
            }
            if "attempt_id" not in lease_columns:
                connection.executescript(
                    """
                    ALTER TABLE worker_lease RENAME TO worker_lease_legacy;
                    CREATE TABLE worker_lease (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        worker_id TEXT NOT NULL,
                        attempt_id TEXT NOT NULL UNIQUE REFERENCES task_attempts(attempt_id),
                        expires_at TEXT NOT NULL
                    );
                    INSERT INTO worker_lease(singleton, worker_id, attempt_id, expires_at)
                    SELECT legacy.singleton, legacy.worker_id, attempts.attempt_id, legacy.expires_at
                    FROM worker_lease_legacy AS legacy
                    JOIN task_attempts AS attempts
                      ON attempts.task_id = legacy.task_id
                     AND attempts.attempt_number = (
                         SELECT MAX(newest.attempt_number)
                         FROM task_attempts AS newest
                         WHERE newest.task_id = legacy.task_id
                     );
                    DROP TABLE worker_lease_legacy;
                    """
                )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS tasks_status_queue
                    ON tasks(status, queue_seq);
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_batch_instance
                    ON tasks(batch_id, instance_id)
                    WHERE batch_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_batch_source_index
                    ON tasks(batch_id, source_index)
                    WHERE batch_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS usage_snapshots_observed
                    ON usage_snapshots(observed_at, snapshot_seq);
                CREATE INDEX IF NOT EXISTS batch_control_events_batch_sequence
                    ON batch_control_events(batch_id, event_seq);
                CREATE INDEX IF NOT EXISTS task_attempts_task_number
                    ON task_attempts(task_id, attempt_number);
                CREATE INDEX IF NOT EXISTS task_attempts_status_sequence
                    ON task_attempts(status, attempt_seq);
                """
            )

    def create_batch(
        self,
        request: BatchCreate,
        instance_ids: Sequence[str],
        *,
        now: datetime,
    ) -> tuple[BatchRecord, bool]:
        """Create a durable batch and all of its queued children atomically."""

        ordered_ids = tuple(instance_ids)
        if not ordered_ids:
            raise ValueError("A batch must contain at least one instance")
        if len(set(ordered_ids)) != len(ordered_ids):
            raise ValueError("A batch cannot contain duplicate instance IDs")
        created_at = _timestamp(now)
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM batches WHERE idempotency_key = ?",
                (request.idempotency_key,),
            ).fetchone()
            if existing is not None:
                return self._batch_record(connection, existing), False

            placeholder = f"pending-batch-{uuid.uuid4().hex}"
            cursor = connection.execute(
                """
                INSERT INTO batches(
                    batch_id, idempotency_key, request_json, total_tasks, created_at,
                    control_intent, usage_pause_percent, control_updated_at, control_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    placeholder,
                    request.idempotency_key,
                    request.model_dump_json(),
                    len(ordered_ids),
                    created_at,
                    BatchControlIntent.RUN.value,
                    request.usage_pause_percent,
                    created_at,
                    0,
                ),
            )
            sequence = cursor.lastrowid
            if sequence is None:  # pragma: no cover - SQLite guarantees this for INTEGER PRIMARY KEY
                raise TaskStoreError("SQLite did not assign a batch sequence")
            batch_id = _batch_id(now, sequence)
            connection.execute(
                "UPDATE batches SET batch_id = ? WHERE batch_seq = ?",
                (batch_id, sequence),
            )
            for source_index, instance_id in enumerate(ordered_ids):
                task_id = _batch_task_id(now, sequence, source_index)
                child = TaskCreate(
                    powercontext_ref=request.powercontext_ref,
                    benchmark=request.benchmark,
                    instance_id=instance_id,
                    model=request.model,
                    reasoning_effort=request.reasoning_effort,
                    treatment_mode=request.treatment_mode,
                    idempotency_key=f"{batch_id}.{source_index:04d}",
                )
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, idempotency_key, request_json, batch_id, instance_id,
                        source_index, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        child.idempotency_key,
                        child.model_dump_json(),
                        batch_id,
                        instance_id,
                        source_index,
                        TaskStatus.QUEUED.value,
                        created_at,
                    ),
                )
                self._insert_initial_attempt(
                    connection,
                    task_id=task_id,
                    idempotency_key=f"{task_id}.attempt-0001",
                    created_at=created_at,
                )
            self._append_control_event(
                connection,
                batch_id,
                BatchControlEventType.BATCH_CREATED,
                "system",
                {"usage_pause_percent": request.usage_pause_percent},
                now,
            )
            return self._batch_record(connection, self._select_batch(connection, batch_id)), True

    def get_batch(self, batch_id: str) -> BatchRecord:
        """Return one batch with lifecycle state derived from its children."""

        with self._connection() as connection:
            return self._batch_record(connection, self._select_batch(connection, batch_id))

    def save_usage_snapshot(self, snapshot: UsageSnapshot) -> UsageSnapshot:
        """Append one normalized account-wide usage observation."""

        observed_at = _timestamp(snapshot.observed_at)
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO usage_snapshots(snapshot_json, observed_at)
                VALUES (?, ?)
                """,
                (snapshot.model_dump_json(), observed_at),
            )
        return snapshot

    def latest_usage_snapshot(self) -> UsageSnapshot | None:
        """Return the newest immutable usage observation, if one exists."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT snapshot_json
                FROM usage_snapshots
                ORDER BY snapshot_seq DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return UsageSnapshot.model_validate_json(row["snapshot_json"], strict=True)

    def list_control_events(self, batch_id: str) -> tuple[BatchControlEvent, ...]:
        """Return the sanitized control audit trail in insertion order."""

        with self._connection() as connection:
            self._select_batch(connection, batch_id)
            rows = connection.execute(
                """
                SELECT *
                FROM batch_control_events
                WHERE batch_id = ?
                ORDER BY event_seq ASC
                """,
                (batch_id,),
            ).fetchall()
        return tuple(self._control_event(row) for row in rows)

    def request_pause(
        self,
        batch_id: str,
        *,
        reason: BatchPauseReason,
        now: datetime,
    ) -> BatchRecord:
        """Persist a pause intent and stop only at a benchmark-task boundary."""

        if not isinstance(reason, BatchPauseReason):
            raise TypeError("reason must be a BatchPauseReason")
        now_text = _timestamp(now)
        with self._write() as connection:
            row = self._select_batch(connection, batch_id)
            intent = BatchControlIntent(row["control_intent"])
            if intent is BatchControlIntent.PAUSE:
                return self._batch_record(connection, row)
            if intent is BatchControlIntent.CANCEL:
                raise TaskConflict("A cancelling batch cannot be paused")
            if self._all_batch_tasks_terminal(connection, batch_id):
                raise TaskConflict("A completed batch cannot be paused")

            connection.execute(
                """
                UPDATE batches
                SET control_intent = ?, pause_reason = ?, control_updated_at = ?,
                    control_version = control_version + 1
                WHERE batch_id = ?
                """,
                (BatchControlIntent.PAUSE.value, reason.value, now_text, batch_id),
            )
            event_type, actor = _pause_event(reason)
            self._append_control_event(connection, batch_id, event_type, actor, {}, now)
            self._finalize_batch_intent(connection, batch_id, now=now)
            return self._batch_record(connection, self._select_batch(connection, batch_id))

    def request_resume(
        self,
        batch_id: str,
        *,
        snapshot: UsageSnapshot,
        now: datetime,
    ) -> BatchRecord:
        """Resume a paused batch only below its configured usage threshold."""

        _timestamp(now)
        with self._write() as connection:
            row = self._select_batch(connection, batch_id)
            intent = BatchControlIntent(row["control_intent"])
            if intent is BatchControlIntent.RUN:
                return self._batch_record(connection, row)
            if intent is BatchControlIntent.CANCEL:
                raise TaskConflict("A cancelling batch cannot be resumed")
            if self._all_batch_tasks_terminal(connection, batch_id):
                raise TaskConflict("A completed batch cannot be resumed")
            threshold = _stored_int(row["usage_pause_percent"], name="usage threshold")
            if snapshot.used_percent >= threshold:
                raise TaskConflict("Current Codex usage is at or above the batch threshold")

            self._append_control_event(
                connection,
                batch_id,
                BatchControlEventType.RESUME_REQUESTED,
                "user",
                {"used_percent": snapshot.used_percent, "threshold_percent": threshold},
                now,
            )
            connection.execute(
                """
                UPDATE batches
                SET control_intent = ?, pause_reason = NULL, control_updated_at = ?,
                    control_version = control_version + 1
                WHERE batch_id = ?
                """,
                (BatchControlIntent.RUN.value, _timestamp(now), batch_id),
            )
            self._append_control_event(
                connection,
                batch_id,
                BatchControlEventType.RESUMED,
                "system",
                {"used_percent": snapshot.used_percent, "threshold_percent": threshold},
                now,
            )
            return self._batch_record(connection, self._select_batch(connection, batch_id))

    def request_cancel(self, batch_id: str, *, now: datetime) -> BatchRecord:
        """Persist cancellation and cancel queued work once no child is running."""

        now_text = _timestamp(now)
        with self._write() as connection:
            row = self._select_batch(connection, batch_id)
            intent = BatchControlIntent(row["control_intent"])
            if intent is BatchControlIntent.CANCEL:
                self._finalize_batch_intent(connection, batch_id, now=now)
                return self._batch_record(connection, self._select_batch(connection, batch_id))
            if self._all_batch_tasks_terminal(connection, batch_id):
                raise TaskConflict("A completed batch cannot be cancelled")

            connection.execute(
                """
                UPDATE batches
                SET control_intent = ?, pause_reason = NULL, control_updated_at = ?,
                    control_version = control_version + 1
                WHERE batch_id = ?
                """,
                (BatchControlIntent.CANCEL.value, now_text, batch_id),
            )
            self._append_control_event(
                connection,
                batch_id,
                BatchControlEventType.CANCEL_REQUESTED,
                "user",
                {},
                now,
            )
            self._finalize_batch_intent(connection, batch_id, now=now)
            return self._batch_record(connection, self._select_batch(connection, batch_id))

    def update_usage_threshold(
        self,
        batch_id: str,
        *,
        percent: int,
        expected_version: int,
        now: datetime,
    ) -> BatchRecord:
        """Update a threshold with optimistic concurrency and no implicit resume."""

        _validate_percentage(percent)
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        now_text = _timestamp(now)
        with self._write() as connection:
            row = self._select_batch(connection, batch_id)
            version = _stored_int(row["control_version"], name="control version")
            if version != expected_version:
                raise TaskConflict("Batch control version does not match")
            previous = _stored_int(row["usage_pause_percent"], name="usage threshold")
            if previous == percent:
                return self._batch_record(connection, row)

            connection.execute(
                """
                UPDATE batches
                SET usage_pause_percent = ?, control_updated_at = ?,
                    control_version = control_version + 1
                WHERE batch_id = ?
                """,
                (percent, now_text, batch_id),
            )
            self._append_control_event(
                connection,
                batch_id,
                BatchControlEventType.THRESHOLD_CHANGED,
                "user",
                {"from_percent": previous, "to_percent": percent},
                now,
            )
            return self._batch_record(connection, self._select_batch(connection, batch_id))

    def finalize_batch_intent_after_attempt(self, batch_id: str, *, now: datetime) -> BatchRecord:
        """Apply a pending pause or cancel after the active benchmark task ends."""

        _timestamp(now)
        with self._write() as connection:
            self._select_batch(connection, batch_id)
            self._finalize_batch_intent(connection, batch_id, now=now)
            return self._batch_record(connection, self._select_batch(connection, batch_id))

    def pin_batch_revision(self, batch_id: str, sha: str) -> BatchRecord:
        """Persist the one immutable PowerContext revision shared by all children."""

        if fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise ValueError("Pinned PowerContext SHA must be 40 lowercase hexadecimal characters")
        with self._write() as connection:
            row = self._select_batch(connection, batch_id)
            existing = row["resolved_powercontext_sha"]
            if existing is not None and existing != sha:
                raise TaskConflict("Batch is already pinned to a different PowerContext revision")
            if existing is None:
                connection.execute(
                    "UPDATE batches SET resolved_powercontext_sha = ? WHERE batch_id = ?",
                    (sha, batch_id),
                )
            return self._batch_record(connection, self._select_batch(connection, batch_id))

    def list_batches(self) -> list[BatchRecord]:
        """List batches in stable creation order."""

        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM batches ORDER BY batch_seq ASC").fetchall()
            return [self._batch_record(connection, row) for row in rows]

    def list_batch_tasks(self, batch_id: str) -> list[TaskRecord]:
        """List every child task in immutable dataset source order."""

        with self._connection() as connection:
            self._select_batch(connection, batch_id)
            rows = connection.execute(
                "SELECT * FROM tasks WHERE batch_id = ? ORDER BY source_index ASC",
                (batch_id,),
            ).fetchall()
            return [self._record(connection, row) for row in rows]

    def get_batch_task(self, batch_id: str, task_id: str) -> TaskRecord:
        """Return one task only when it belongs to the requested batch."""

        with self._connection() as connection:
            self._select_batch(connection, batch_id)
            row = connection.execute(
                "SELECT * FROM tasks WHERE batch_id = ? AND task_id = ?",
                (batch_id, task_id),
            ).fetchone()
            if row is None:
                raise TaskNotFound(f"Task not found in batch: {task_id}")
            return self._record(connection, row)

    def list_task_attempts(self, batch_id: str, task_id: str) -> tuple[TaskAttemptRecord, ...]:
        """List every immutable execution attempt for one logical batch task."""

        with self._connection() as connection:
            self._select_batch(connection, batch_id)
            task = connection.execute(
                "SELECT 1 FROM tasks WHERE batch_id = ? AND task_id = ?",
                (batch_id, task_id),
            ).fetchone()
            if task is None:
                raise TaskNotFound(f"Task not found in batch: {task_id}")
            rows = connection.execute(
                """
                SELECT *
                FROM task_attempts
                WHERE task_id = ?
                ORDER BY attempt_number ASC
                """,
                (task_id,),
            ).fetchall()
        return tuple(self._attempt_record(row) for row in rows)

    def retry_failed_task(
        self,
        batch_id: str,
        task_id: str,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[TaskAttemptRecord, bool]:
        """Create one new queued attempt without modifying retained failures."""

        if not isinstance(idempotency_key, str) or fullmatch(r"[A-Za-z0-9._-]{8,128}", idempotency_key) is None:
            raise ValueError("Retry idempotency key is invalid")
        created_at = _timestamp(now)
        with self._write() as connection:
            self._select_batch(connection, batch_id)
            task = connection.execute(
                "SELECT * FROM tasks WHERE batch_id = ? AND task_id = ?",
                (batch_id, task_id),
            ).fetchone()
            if task is None:
                raise TaskNotFound(f"Task not found in batch: {task_id}")
            existing = connection.execute(
                "SELECT * FROM task_attempts WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["task_id"] != task_id:
                    raise TaskConflict("Retry idempotency key belongs to another task")
                return self._attempt_record(existing), False

            latest = self._select_latest_attempt(connection, task_id)
            latest_record = self._attempt_record(latest)
            if not latest_record.retryable:
                raise TaskConflict("The current task outcome is not retryable")
            attempt_number = latest_record.attempt_number + 1
            attempt_id = f"{task_id}.attempt-{attempt_number:04d}"
            connection.execute(
                """
                INSERT INTO task_attempts(
                    attempt_id, task_id, attempt_number, idempotency_key,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    task_id,
                    attempt_number,
                    idempotency_key,
                    TaskStatus.QUEUED.value,
                    created_at,
                ),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, phase = NULL, started_at = NULL, finished_at = NULL,
                    version = 0, failure_category = NULL, failure_phase = NULL,
                    failure_summary = NULL, result_json = NULL
                WHERE task_id = ?
                """,
                (TaskStatus.QUEUED.value, task_id),
            )
            connection.execute(
                """
                UPDATE batches
                SET control_intent = ?, pause_reason = NULL, control_updated_at = ?,
                    control_version = control_version + 1
                WHERE batch_id = ?
                """,
                (BatchControlIntent.RUN.value, created_at, batch_id),
            )
            self._append_control_event(
                connection,
                batch_id,
                BatchControlEventType.TASK_RETRY_REQUESTED,
                "user",
                {"task_id": task_id, "attempt_number": attempt_number},
                now,
            )
            return self._attempt_record(self._select_latest_attempt(connection, task_id)), True

    def cancel_batch_queued(self, batch_id: str, *, now: datetime) -> BatchRecord:
        """Compatibility alias for the durable boundary-based cancellation action."""

        return self.request_cancel(batch_id, now=now)

    def create(self, request: TaskCreate, *, now: datetime) -> tuple[TaskRecord, bool]:
        """Create a queued task, or replay the task for an idempotency key."""
        created_at = _timestamp(now)
        request_json = request.model_dump_json()
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?",
                (request.idempotency_key,),
            ).fetchone()
            if existing is not None:
                return self._record(connection, existing), False

            placeholder = f"pending-{uuid.uuid4().hex}"
            cursor = connection.execute(
                """
                INSERT INTO tasks (
                    task_id, idempotency_key, request_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (placeholder, request.idempotency_key, request_json, TaskStatus.QUEUED.value, created_at),
            )
            sequence = cursor.lastrowid
            if sequence is None:  # pragma: no cover - sqlite guarantees this for an INTEGER PRIMARY KEY
                raise TaskStoreError("SQLite did not assign a task sequence")
            task_id = _task_id(now, sequence)
            connection.execute(
                "UPDATE tasks SET task_id = ? WHERE queue_seq = ?",
                (task_id, sequence),
            )
            self._insert_initial_attempt(
                connection,
                task_id=task_id,
                idempotency_key=f"{task_id}.attempt-0001",
                created_at=created_at,
            )
            row = self._select_task(connection, task_id)
            return self._record(connection, row), True

    def get(self, task_id: str) -> TaskRecord:
        """Return one task or raise :class:`TaskNotFound`."""
        with self._connection() as connection:
            return self._record(connection, self._select_task(connection, task_id))

    def list_tasks(
        self,
        *,
        status: TaskStatus | None,
        order: Literal["oldest", "newest"] = "oldest",
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskSummary]:
        """List tasks in a stable requested creation order."""
        if limit < 1:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must not be negative")
        if order not in ("oldest", "newest"):
            raise ValueError("order must be oldest or newest")
        sql = "SELECT * FROM tasks"
        parameters: list[object] = []
        if status is not None:
            sql += " WHERE status = ?"
            parameters.append(status.value)
        sql += " ORDER BY queue_seq ASC" if order == "oldest" else " ORDER BY queue_seq DESC"
        sql += " LIMIT ? OFFSET ?"
        parameters.extend((limit, offset))
        with self._connection() as connection:
            return [
                self._summary(self._record(connection, row)) for row in connection.execute(sql, parameters).fetchall()
            ]

    def queue_position(self, task_id: str) -> int | None:
        """Return the one-based position among currently queued tasks."""
        with self._connection() as connection:
            row = self._select_task(connection, task_id)
            if TaskStatus(row["status"]) is not TaskStatus.QUEUED:
                return None
            position = connection.execute(
                """
                SELECT COUNT(*) FROM tasks
                WHERE status = ? AND queue_seq <= ?
                """,
                (TaskStatus.QUEUED.value, row["queue_seq"]),
            ).fetchone()[0]
            if not isinstance(position, int):
                raise TypeError("SQLite queue count is not an integer")
            return position

    def health_snapshot(self, *, now: datetime) -> HealthSnapshot:
        """Return queue counts and observable lease state without mutating tasks."""
        now_text = _timestamp(now)
        with self._connection() as connection:
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status").fetchall()
            }
            lease = connection.execute(
                "SELECT 1 FROM worker_lease WHERE singleton = ? AND expires_at > ?",
                (1, now_text),
            ).fetchone()
        return {
            "worker_lease_active": lease is not None,
            "queued_tasks": counts.get(TaskStatus.QUEUED.value, 0),
            "running_tasks": counts.get(TaskStatus.RUNNING.value, 0),
        }

    def cancel_queued(self, task_id: str, *, now: datetime) -> TaskRecord:
        """Cancel a queued task."""
        finished_at = _timestamp(now)
        with self._write() as connection:
            row = self._select_task(connection, task_id)
            if TaskStatus(row["status"]) is not TaskStatus.QUEUED:
                raise TaskConflict("Only queued tasks can be cancelled")
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, version = version + 1
                WHERE task_id = ?
                """,
                (TaskStatus.CANCELLED.value, finished_at, task_id),
            )
            attempt = self._select_latest_attempt(connection, task_id)
            connection.execute(
                """
                UPDATE task_attempts
                SET status = ?, finished_at = ?, version = version + 1
                WHERE attempt_id = ? AND status = ?
                """,
                (
                    TaskStatus.CANCELLED.value,
                    finished_at,
                    attempt["attempt_id"],
                    TaskStatus.QUEUED.value,
                ),
            )
            return self._record(connection, self._select_task(connection, task_id))

    def claim_next(self, worker_id: str, *, now: datetime) -> TaskRecord | None:
        """Atomically acquire the global lease and claim the oldest queued task."""
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        _timestamp(now)
        with self._write() as connection:
            return self._claim_next(connection, worker_id, now=now, allow_standalone=True)

    def claim_next_with_usage(
        self,
        worker_id: str,
        *,
        snapshot: UsageSnapshot,
        default_threshold: int,
        now: datetime,
    ) -> TaskRecord | None:
        """Persist one snapshot, pause protected batches, and claim atomically."""

        if not worker_id:
            raise ValueError("worker_id must not be empty")
        _validate_percentage(default_threshold)
        _timestamp(now)
        with self._write() as connection:
            self._save_usage_snapshot(connection, snapshot)
            self._apply_usage_snapshot(connection, snapshot, now=now)
            allow_standalone = snapshot.rate_limit_reached_type is None and snapshot.used_percent < default_threshold
            return self._claim_next(
                connection,
                worker_id,
                now=now,
                allow_standalone=allow_standalone,
            )

    def apply_usage_snapshot(self, snapshot: UsageSnapshot, *, now: datetime) -> None:
        """Persist an observation and apply its pause consequences without claiming."""

        _timestamp(now)
        with self._write() as connection:
            self._save_usage_snapshot(connection, snapshot)
            self._apply_usage_snapshot(connection, snapshot, now=now)

    def pause_runnable_batches(self, *, reason: BatchPauseReason, now: datetime) -> None:
        """Fail closed by pausing every currently runnable batch."""

        if not isinstance(reason, BatchPauseReason):
            raise TypeError("reason must be a BatchPauseReason")
        _timestamp(now)
        with self._write() as connection:
            self._pause_runnable_batches(connection, reason=reason, now=now)

    def _claim_next(
        self,
        connection: sqlite3.Connection,
        worker_id: str,
        *,
        now: datetime,
        allow_standalone: bool,
    ) -> TaskRecord | None:
        now_text = _timestamp(now)
        running = connection.execute(
            "SELECT 1 FROM tasks WHERE status = ? LIMIT 1",
            (TaskStatus.RUNNING.value,),
        ).fetchone()
        if running is not None:
            return None
        lease = connection.execute("SELECT * FROM worker_lease WHERE singleton = ?", (1,)).fetchone()
        if lease is not None and lease["expires_at"] > now_text and lease["worker_id"] != worker_id:
            return None

        row = connection.execute(
            """
            SELECT tasks.*
            FROM tasks
            LEFT JOIN batches ON batches.batch_id = tasks.batch_id
            WHERE tasks.status = ?
              AND (
                  (tasks.batch_id IS NULL AND ?)
                  OR (tasks.batch_id IS NOT NULL AND batches.control_intent = ?)
              )
            ORDER BY tasks.queue_seq ASC
            LIMIT 1
            """,
            (
                TaskStatus.QUEUED.value,
                int(allow_standalone),
                BatchControlIntent.RUN.value,
            ),
        ).fetchone()
        if row is None:
            if lease is not None and lease["expires_at"] <= now_text:
                connection.execute("DELETE FROM worker_lease WHERE singleton = ?", (1,))
            return None

        task_id = str(row["task_id"])
        attempt = self._select_latest_attempt(connection, task_id)
        effective_claim_time = max(now, _parse_timestamp(attempt["created_at"]))
        claim_time_text = _timestamp(effective_claim_time)
        expires_at = _timestamp(effective_claim_time + self._lease_duration)
        connection.execute(
            """
            INSERT INTO worker_lease(singleton, worker_id, attempt_id, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                worker_id = excluded.worker_id,
                attempt_id = excluded.attempt_id,
                expires_at = excluded.expires_at
            """,
            (1, worker_id, attempt["attempt_id"], expires_at),
        )
        connection.execute(
            """
            UPDATE tasks
            SET status = ?, started_at = ?, version = version + 1
            WHERE task_id = ? AND status = ?
            """,
            (TaskStatus.RUNNING.value, claim_time_text, task_id, TaskStatus.QUEUED.value),
        )
        connection.execute(
            """
            UPDATE task_attempts
            SET status = ?, started_at = ?, version = version + 1
            WHERE attempt_id = ? AND status = ?
            """,
            (
                TaskStatus.RUNNING.value,
                claim_time_text,
                attempt["attempt_id"],
                TaskStatus.QUEUED.value,
            ),
        )
        return self._record(connection, self._select_task(connection, task_id))

    @staticmethod
    def _save_usage_snapshot(connection: sqlite3.Connection, snapshot: UsageSnapshot) -> None:
        connection.execute(
            """
            INSERT INTO usage_snapshots(snapshot_json, observed_at)
            VALUES (?, ?)
            """,
            (snapshot.model_dump_json(), _timestamp(snapshot.observed_at)),
        )

    def _apply_usage_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: UsageSnapshot,
        *,
        now: datetime,
    ) -> None:
        if snapshot.rate_limit_reached_type is not None:
            self._pause_runnable_batches(
                connection,
                reason=BatchPauseReason.QUOTA_LIMIT,
                now=now,
            )
            return
        self._pause_runnable_batches(
            connection,
            reason=BatchPauseReason.USAGE_THRESHOLD,
            now=now,
            used_percent=snapshot.used_percent,
        )

    def _pause_runnable_batches(
        self,
        connection: sqlite3.Connection,
        *,
        reason: BatchPauseReason,
        now: datetime,
        used_percent: int | None = None,
    ) -> None:
        sql = """
            SELECT *
            FROM batches
            WHERE control_intent = ?
              AND EXISTS (
                  SELECT 1
                  FROM tasks
                  WHERE tasks.batch_id = batches.batch_id
                    AND tasks.status IN (?, ?)
              )
        """
        parameters: list[object] = [
            BatchControlIntent.RUN.value,
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value,
        ]
        if used_percent is not None:
            sql += " AND usage_pause_percent <= ?"
            parameters.append(used_percent)
        sql += " ORDER BY batch_seq ASC"
        rows = connection.execute(sql, parameters).fetchall()
        for row in rows:
            batch_id = str(row["batch_id"])
            connection.execute(
                """
                UPDATE batches
                SET control_intent = ?, pause_reason = ?, control_updated_at = ?,
                    control_version = control_version + 1
                WHERE batch_id = ?
                """,
                (
                    BatchControlIntent.PAUSE.value,
                    reason.value,
                    _timestamp(now),
                    batch_id,
                ),
            )
            event_type, actor = _pause_event(reason)
            details = (
                {
                    "used_percent": used_percent,
                    "threshold_percent": _stored_int(
                        row["usage_pause_percent"],
                        name="usage threshold",
                    ),
                }
                if used_percent is not None
                else {}
            )
            self._append_control_event(
                connection,
                batch_id,
                event_type,
                actor,
                details,
                now,
            )
            self._finalize_batch_intent(connection, batch_id, now=now)

    def heartbeat(self, task_id: str, worker_id: str, *, now: datetime) -> TaskRecord:
        """Renew the active lease owned by a worker."""
        with self._write() as connection:
            self._require_running_owner(connection, task_id, worker_id, now=now)
            attempt = self._select_latest_attempt(connection, task_id)
            started_at = _parse_optional_timestamp(attempt["started_at"])
            if started_at is None:
                raise TaskConflict("Running task has no start time")
            lease = connection.execute("SELECT expires_at FROM worker_lease WHERE singleton = ?", (1,)).fetchone()
            if lease is None:
                raise TaskOwnershipError("Worker lease is not active")
            expires_at = _timestamp(
                max(
                    _parse_timestamp(lease["expires_at"]),
                    max(now, started_at) + self._lease_duration,
                )
            )
            connection.execute(
                "UPDATE worker_lease SET expires_at = ? WHERE singleton = ?",
                (expires_at, 1),
            )
            connection.execute(
                "UPDATE tasks SET version = version + 1 WHERE task_id = ?",
                (task_id,),
            )
            connection.execute(
                "UPDATE task_attempts SET version = version + 1 WHERE attempt_id = ?",
                (attempt["attempt_id"],),
            )
            return self._record(connection, self._select_task(connection, task_id))

    def set_phase(
        self,
        task_id: str,
        worker_id: str,
        phase: TaskPhase,
        *,
        now: datetime,
    ) -> TaskRecord:
        """Set the current phase of a running task."""
        with self._write() as connection:
            self._require_running_owner(connection, task_id, worker_id, now=now)
            attempt = self._select_latest_attempt(connection, task_id)
            connection.execute(
                "UPDATE tasks SET phase = ?, version = version + 1 WHERE task_id = ?",
                (phase.value, task_id),
            )
            connection.execute(
                "UPDATE task_attempts SET phase = ?, version = version + 1 WHERE attempt_id = ?",
                (phase.value, attempt["attempt_id"]),
            )
            return self._record(connection, self._select_task(connection, task_id))

    def succeed(
        self,
        task_id: str,
        worker_id: str,
        result: TaskResult,
        *,
        now: datetime,
    ) -> TaskRecord:
        """Complete an owned running task successfully."""
        finished_at = _timestamp(now)
        with self._write() as connection:
            self._require_running_owner(connection, task_id, worker_id, now=now)
            attempt = self._select_latest_attempt(connection, task_id)
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, result_json = ?, version = version + 1
                WHERE task_id = ?
                """,
                (TaskStatus.SUCCEEDED.value, finished_at, result.model_dump_json(), task_id),
            )
            connection.execute(
                """
                UPDATE task_attempts
                SET status = ?, finished_at = ?, result_json = ?, version = version + 1
                WHERE attempt_id = ?
                """,
                (
                    TaskStatus.SUCCEEDED.value,
                    finished_at,
                    result.model_dump_json(),
                    attempt["attempt_id"],
                ),
            )
            connection.execute("DELETE FROM worker_lease WHERE singleton = ?", (1,))
            return self._record(connection, self._select_task(connection, task_id))

    def fail(
        self,
        task_id: str,
        worker_id: str,
        failure: SafeFailure,
        *,
        now: datetime,
    ) -> TaskRecord:
        """Complete an owned running task with a safe failure."""
        finished_at = _timestamp(now)
        phase = failure.phase.value if failure.phase is not None else None
        with self._write() as connection:
            self._require_running_owner(connection, task_id, worker_id, now=now)
            attempt = self._select_latest_attempt(connection, task_id)
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, phase = ?, finished_at = ?, failure_category = ?,
                    failure_phase = ?, failure_summary = ?, version = version + 1
                WHERE task_id = ?
                """,
                (
                    TaskStatus.FAILED.value,
                    phase,
                    finished_at,
                    failure.category.value,
                    phase,
                    failure.summary,
                    task_id,
                ),
            )
            connection.execute(
                """
                UPDATE task_attempts
                SET status = ?, phase = ?, finished_at = ?, failure_category = ?,
                    failure_phase = ?, failure_summary = ?, version = version + 1
                WHERE attempt_id = ?
                """,
                (
                    TaskStatus.FAILED.value,
                    phase,
                    finished_at,
                    failure.category.value,
                    phase,
                    failure.summary,
                    attempt["attempt_id"],
                ),
            )
            connection.execute("DELETE FROM worker_lease WHERE singleton = ?", (1,))
            return self._record(connection, self._select_task(connection, task_id))

    def recover_expired(self, *, now: datetime) -> list[str]:
        """Interrupt the running task whose singleton lease has expired."""
        now_text = _timestamp(now)
        with self._write() as connection:
            lease = connection.execute("SELECT * FROM worker_lease WHERE singleton = ?", (1,)).fetchone()
            if lease is None or lease["expires_at"] > now_text:
                return []
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE attempt_id = ?",
                (lease["attempt_id"],),
            ).fetchone()
            if attempt is None:
                raise TaskStoreError("Worker lease references a missing attempt")
            task_id = str(attempt["task_id"])
            row = self._select_task(connection, task_id)
            recovered: list[str] = []
            if TaskStatus(row["status"]) is TaskStatus.RUNNING:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, finished_at = ?, failure_category = ?,
                        failure_phase = phase, failure_summary = ?, version = version + 1
                    WHERE task_id = ?
                    """,
                    (
                        TaskStatus.INTERRUPTED.value,
                        now_text,
                        FailureCategory.WORKER_INTERRUPTION.value,
                        "Evaluation worker lease expired",
                        task_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE task_attempts
                    SET status = ?, finished_at = ?, failure_category = ?,
                        failure_phase = phase, failure_summary = ?, version = version + 1
                    WHERE attempt_id = ?
                    """,
                    (
                        TaskStatus.INTERRUPTED.value,
                        now_text,
                        FailureCategory.WORKER_INTERRUPTION.value,
                        "Evaluation worker lease expired",
                        attempt["attempt_id"],
                    ),
                )
                recovered.append(task_id)
            connection.execute("DELETE FROM worker_lease WHERE singleton = ?", (1,))
            return recovered

    def _finalize_batch_intent(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        *,
        now: datetime,
    ) -> None:
        running = connection.execute(
            "SELECT 1 FROM tasks WHERE batch_id = ? AND status = ? LIMIT 1",
            (batch_id, TaskStatus.RUNNING.value),
        ).fetchone()
        if running is not None:
            return

        row = self._select_batch(connection, batch_id)
        intent = BatchControlIntent(row["control_intent"])
        if intent is BatchControlIntent.CANCEL:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, version = version + 1
                WHERE batch_id = ? AND status = ?
                """,
                (
                    TaskStatus.CANCELLED.value,
                    _timestamp(now),
                    batch_id,
                    TaskStatus.QUEUED.value,
                ),
            )
            connection.execute(
                """
                UPDATE task_attempts
                SET status = ?, finished_at = ?, version = version + 1
                WHERE status = ?
                  AND task_id IN (
                      SELECT task_id FROM tasks WHERE batch_id = ?
                  )
                  AND attempt_number = (
                      SELECT MAX(newest.attempt_number)
                      FROM task_attempts AS newest
                      WHERE newest.task_id = task_attempts.task_id
                  )
                """,
                (
                    TaskStatus.CANCELLED.value,
                    _timestamp(now),
                    TaskStatus.QUEUED.value,
                    batch_id,
                ),
            )
            self._append_control_event_once(
                connection,
                batch_id,
                BatchControlEventType.CANCELLED,
                "system",
                {},
                now,
            )
        elif intent is BatchControlIntent.PAUSE:
            self._append_control_event_once(
                connection,
                batch_id,
                BatchControlEventType.PAUSED,
                "system",
                {},
                now,
            )
        elif self._all_batch_tasks_terminal(connection, batch_id):
            self._append_control_event_once(
                connection,
                batch_id,
                BatchControlEventType.BATCH_COMPLETED,
                "system",
                {},
                now,
            )

    @staticmethod
    def _all_batch_tasks_terminal(connection: sqlite3.Connection, batch_id: str) -> bool:
        nonterminal = connection.execute(
            """
            SELECT 1
            FROM tasks
            WHERE batch_id = ?
              AND status NOT IN (?, ?, ?, ?)
            LIMIT 1
            """,
            (
                batch_id,
                TaskStatus.SUCCEEDED.value,
                TaskStatus.FAILED.value,
                TaskStatus.INTERRUPTED.value,
                TaskStatus.CANCELLED.value,
            ),
        ).fetchone()
        return nonterminal is None

    @staticmethod
    def _append_control_event(
        connection: sqlite3.Connection,
        batch_id: str,
        event_type: BatchControlEventType,
        actor: Literal["user", "system"],
        details: dict[str, int | str | None],
        now: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO batch_control_events(
                batch_id, event_type, actor, details_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                event_type.value,
                actor,
                json.dumps(details, sort_keys=True, separators=(",", ":")),
                _timestamp(now),
            ),
        )

    @classmethod
    def _append_control_event_once(
        cls,
        connection: sqlite3.Connection,
        batch_id: str,
        event_type: BatchControlEventType,
        actor: Literal["user", "system"],
        details: dict[str, int | str | None],
        now: datetime,
    ) -> None:
        existing = connection.execute(
            """
            SELECT event_type
            FROM batch_control_events
            WHERE batch_id = ?
            ORDER BY event_seq DESC
            LIMIT 1
            """,
            (batch_id,),
        ).fetchone()
        if existing is None or existing["event_type"] != event_type.value:
            cls._append_control_event(connection, batch_id, event_type, actor, details, now)

    def _require_running_owner(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        worker_id: str,
        *,
        now: datetime,
    ) -> None:
        attempt = self._select_latest_attempt(connection, task_id)
        if TaskStatus(attempt["status"]) is not TaskStatus.RUNNING:
            raise TaskConflict("Task is not running")
        lease = connection.execute("SELECT * FROM worker_lease WHERE singleton = ?", (1,)).fetchone()
        if (
            lease is None
            or lease["attempt_id"] != attempt["attempt_id"]
            or lease["worker_id"] != worker_id
            or lease["expires_at"] <= _timestamp(now)
        ):
            raise TaskOwnershipError("Worker does not own an active lease for this task")

    @staticmethod
    def _select_task(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(f"Task not found: {task_id}")
        return row

    @staticmethod
    def _select_latest_attempt(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT *
            FROM task_attempts
            WHERE task_id = ?
            ORDER BY attempt_number DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise TaskStoreError(f"Task has no execution attempt: {task_id}")
        return row

    @staticmethod
    def _insert_initial_attempt(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        idempotency_key: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_attempts(
                attempt_id, task_id, attempt_number, idempotency_key,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"{task_id}.attempt-0001",
                task_id,
                1,
                idempotency_key,
                TaskStatus.QUEUED.value,
                created_at,
            ),
        )

    @staticmethod
    def _select_batch(connection: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None:
            raise BatchNotFound(f"Batch not found: {batch_id}")
        return row

    @staticmethod
    def _control_event(row: sqlite3.Row) -> BatchControlEvent:
        details = json.loads(row["details_json"])
        if not isinstance(details, dict):
            raise TypeError("Stored control event details are not an object")
        return BatchControlEvent.model_validate(
            {
                "sequence": row["event_seq"],
                "batch_id": row["batch_id"],
                "event_type": BatchControlEventType(row["event_type"]),
                "actor": row["actor"],
                "details": details,
                "occurred_at": _parse_timestamp(row["occurred_at"]),
            },
            strict=True,
        )

    @staticmethod
    def _batch_record(connection: sqlite3.Connection, row: sqlite3.Row) -> BatchRecord:
        batch_id = row["batch_id"]
        if not isinstance(batch_id, str):
            raise TypeError("Stored batch ID is not text")
        child_rows = connection.execute(
            """
            SELECT attempts.status, attempts.started_at, attempts.finished_at
            FROM tasks
            JOIN task_attempts AS attempts
              ON attempts.task_id = tasks.task_id
             AND attempts.attempt_number = (
                 SELECT MAX(newest.attempt_number)
                 FROM task_attempts AS newest
                 WHERE newest.task_id = tasks.task_id
             )
            WHERE tasks.batch_id = ?
            ORDER BY tasks.source_index ASC
            """,
            (batch_id,),
        ).fetchall()
        statuses = tuple(TaskStatus(child["status"]) for child in child_rows)
        intent = BatchControlIntent(row["control_intent"])
        status = derive_controlled_batch_status(intent=intent, task_statuses=statuses)
        starts = [_parse_timestamp(child["started_at"]) for child in child_rows if child["started_at"] is not None]
        finishes = [_parse_timestamp(child["finished_at"]) for child in child_rows if child["finished_at"] is not None]
        terminal = status in {BatchStatus.COMPLETED, BatchStatus.CANCELLED}
        pause_reason = row["pause_reason"]
        control = BatchControlState(
            intent=intent,
            usage_pause_percent=_stored_int(row["usage_pause_percent"], name="usage threshold"),
            pause_reason=BatchPauseReason(pause_reason) if pause_reason is not None else None,
            updated_at=_parse_timestamp(row["control_updated_at"]),
            version=_stored_int(row["control_version"], name="control version"),
        )
        return BatchRecord.model_validate(
            {
                "batch_id": batch_id,
                "request": BatchCreate.model_validate_json(row["request_json"], strict=True),
                "total_tasks": row["total_tasks"],
                "status": status,
                "control": control,
                "created_at": _parse_timestamp(row["created_at"]),
                "started_at": min(starts) if starts else None,
                "finished_at": max(finishes) if terminal and finishes else None,
                "resolved_powercontext_sha": row["resolved_powercontext_sha"],
            },
            strict=True,
        )

    @classmethod
    def _record(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> TaskRecord:
        task_id = row["task_id"]
        if not isinstance(task_id, str):
            raise TypeError("Stored task ID is not text")
        EvaluationPaths(Path("."), task_id)
        request = TaskCreate.model_validate_json(row["request_json"], strict=True)
        attempt = cls._select_latest_attempt(connection, task_id)
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM task_attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        if not isinstance(attempt_count, int) or attempt_count < 1:
            raise TypeError("Stored attempt count is invalid")
        result = None
        if attempt["result_json"] is not None:
            result = TaskResult.model_validate_json(attempt["result_json"], strict=True)
        failure = None
        if (
            attempt["failure_category"] is not None
            or attempt["failure_phase"] is not None
            or attempt["failure_summary"] is not None
        ):
            failure = SafeFailure.model_validate(
                {
                    "category": (
                        FailureCategory(attempt["failure_category"])
                        if attempt["failure_category"] is not None
                        else None
                    ),
                    "phase": (TaskPhase(attempt["failure_phase"]) if attempt["failure_phase"] is not None else None),
                    "summary": attempt["failure_summary"],
                },
                strict=True,
            )
        status = TaskStatus(attempt["status"])
        failure_category = failure.category if failure is not None else None
        return TaskRecord.model_validate(
            {
                "task_id": task_id,
                "attempt_id": attempt["attempt_id"],
                "attempt_number": attempt["attempt_number"],
                "attempt_count": attempt_count,
                "retryable": (
                    status in {TaskStatus.FAILED, TaskStatus.INTERRUPTED} and failure_category in RETRYABLE_FAILURES
                ),
                "request": request,
                "status": status,
                "batch_id": row["batch_id"],
                "instance_id": row["instance_id"] or request.instance_id,
                "source_index": row["source_index"],
                "phase": TaskPhase(attempt["phase"]) if attempt["phase"] is not None else None,
                "created_at": _parse_timestamp(attempt["created_at"]),
                "started_at": _parse_optional_timestamp(attempt["started_at"]),
                "finished_at": _parse_optional_timestamp(attempt["finished_at"]),
                "version": attempt["version"],
                "failure_category": failure_category,
                "failure_phase": failure.phase if failure is not None else None,
                "failure_summary": failure.summary if failure is not None else None,
                "result": result,
            },
            strict=True,
        )

    @staticmethod
    def _attempt_record(row: sqlite3.Row) -> TaskAttemptRecord:
        status = TaskStatus(row["status"])
        category = FailureCategory(row["failure_category"]) if row["failure_category"] is not None else None
        result = (
            TaskResult.model_validate_json(row["result_json"], strict=True) if row["result_json"] is not None else None
        )
        return TaskAttemptRecord.model_validate(
            {
                "attempt_id": row["attempt_id"],
                "task_id": row["task_id"],
                "attempt_number": row["attempt_number"],
                "status": status,
                "phase": TaskPhase(row["phase"]) if row["phase"] is not None else None,
                "created_at": _parse_timestamp(row["created_at"]),
                "started_at": _parse_optional_timestamp(row["started_at"]),
                "finished_at": _parse_optional_timestamp(row["finished_at"]),
                "version": row["version"],
                "failure_category": category,
                "failure_phase": (TaskPhase(row["failure_phase"]) if row["failure_phase"] is not None else None),
                "failure_summary": row["failure_summary"],
                "result": result,
                "retryable": (status in {TaskStatus.FAILED, TaskStatus.INTERRUPTED} and category in RETRYABLE_FAILURES),
            },
            strict=True,
        )

    @staticmethod
    def _summary(record: TaskRecord) -> TaskSummary:
        result = record.result
        return TaskSummary(
            task_id=record.task_id,
            attempt_id=record.attempt_id,
            attempt_number=record.attempt_number,
            attempt_count=record.attempt_count,
            retryable=record.retryable,
            powercontext_ref=record.request.powercontext_ref,
            instance_id=record.request.instance_id,
            model=record.request.model,
            status=record.status,
            phase=record.phase,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            version=record.version,
            off_resolved=result.off_resolved if result is not None else None,
            on_resolved=result.on_resolved if result is not None else None,
        )


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Timestamps must use UTC")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("Stored timestamp is not text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("Stored timestamp is not UTC")
    return parsed


def _parse_optional_timestamp(value: Any) -> datetime | None:
    return None if value is None else _parse_timestamp(value)


def _stored_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Stored {name} is not an integer")
    return value


def _validate_percentage(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("percent must be an integer between 1 and 100")


def _pause_event(
    reason: BatchPauseReason,
) -> tuple[BatchControlEventType, Literal["user", "system"]]:
    if reason is BatchPauseReason.USER:
        return BatchControlEventType.PAUSE_REQUESTED, "user"
    if reason is BatchPauseReason.USAGE_THRESHOLD:
        return BatchControlEventType.USAGE_THRESHOLD_REACHED, "system"
    if reason is BatchPauseReason.USAGE_UNAVAILABLE:
        return BatchControlEventType.USAGE_UNAVAILABLE, "system"
    return BatchControlEventType.QUOTA_LIMIT_REACHED, "system"


def _task_id(now: datetime, sequence: int) -> str:
    _timestamp(now)
    task_id = f"run-{now.astimezone(UTC):%Y%m%d-%H%M%S-%f}-{sequence:010d}-{uuid.uuid4().hex[:8]}"
    EvaluationPaths(Path("."), task_id)
    return task_id


def _batch_id(now: datetime, sequence: int) -> str:
    _timestamp(now)
    batch_id = f"batch-{now.astimezone(UTC):%Y%m%d-%H%M%S-%f}-{sequence:010d}-{uuid.uuid4().hex[:8]}"
    EvaluationPaths(Path("."), batch_id)
    return batch_id


def _batch_task_id(now: datetime, batch_sequence: int, source_index: int) -> str:
    _timestamp(now)
    task_id = f"run-{now.astimezone(UTC):%Y%m%d-%H%M%S-%f}-b{batch_sequence:010d}-t{source_index:04d}"
    EvaluationPaths(Path("."), task_id)
    return task_id
