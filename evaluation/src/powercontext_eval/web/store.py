"""Durable SQLite-backed FIFO task queue for the evaluation console."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

from powercontext_eval.paths import EvaluationPaths
from powercontext_eval.web.models import (
    FailureCategory,
    SafeFailure,
    TaskCreate,
    TaskPhase,
    TaskRecord,
    TaskResult,
    TaskStatus,
    TaskSummary,
)


class TaskStoreError(RuntimeError):
    """Base class for task-store domain failures."""


class TaskNotFound(TaskStoreError):
    """The requested task does not exist."""


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
                CREATE TABLE IF NOT EXISTS tasks (
                    queue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_json TEXT NOT NULL,
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
                CREATE INDEX IF NOT EXISTS tasks_status_queue
                    ON tasks(status, queue_seq);
                CREATE TABLE IF NOT EXISTS worker_lease (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    worker_id TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
                    expires_at TEXT NOT NULL
                );
                """
            )

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
                return self._record(existing), False

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
            row = self._select_task(connection, task_id)
            return self._record(row), True

    def get(self, task_id: str) -> TaskRecord:
        """Return one task or raise :class:`TaskNotFound`."""
        with self._connection() as connection:
            return self._record(self._select_task(connection, task_id))

    def list_tasks(
        self,
        *,
        status: TaskStatus | None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskSummary]:
        """List tasks in stable FIFO creation order."""
        if limit < 1:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must not be negative")
        sql = "SELECT * FROM tasks"
        parameters: list[object] = []
        if status is not None:
            sql += " WHERE status = ?"
            parameters.append(status.value)
        sql += " ORDER BY queue_seq ASC LIMIT ? OFFSET ?"
        parameters.extend((limit, offset))
        with self._connection() as connection:
            return [self._summary(self._record(row)) for row in connection.execute(sql, parameters).fetchall()]

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
            return self._record(self._select_task(connection, task_id))

    def claim_next(self, worker_id: str, *, now: datetime) -> TaskRecord | None:
        """Atomically acquire the global lease and claim the oldest queued task."""
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        now_text = _timestamp(now)
        expires_at = _timestamp(now + self._lease_duration)
        with self._write() as connection:
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
                "SELECT * FROM tasks WHERE status = ? ORDER BY queue_seq ASC LIMIT 1",
                (TaskStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                if lease is not None and lease["expires_at"] <= now_text:
                    connection.execute("DELETE FROM worker_lease WHERE singleton = ?", (1,))
                return None

            task_id = str(row["task_id"])
            connection.execute(
                """
                INSERT INTO worker_lease(singleton, worker_id, task_id, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    worker_id = excluded.worker_id,
                    task_id = excluded.task_id,
                    expires_at = excluded.expires_at
                """,
                (1, worker_id, task_id, expires_at),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, started_at = ?, version = version + 1
                WHERE task_id = ? AND status = ?
                """,
                (TaskStatus.RUNNING.value, now_text, task_id, TaskStatus.QUEUED.value),
            )
            return self._record(self._select_task(connection, task_id))

    def heartbeat(self, task_id: str, worker_id: str, *, now: datetime) -> TaskRecord:
        """Renew the active lease owned by a worker."""
        expires_at = _timestamp(now + self._lease_duration)
        with self._write() as connection:
            self._require_running_owner(connection, task_id, worker_id, now=now)
            connection.execute(
                "UPDATE worker_lease SET expires_at = ? WHERE singleton = ?",
                (expires_at, 1),
            )
            connection.execute(
                "UPDATE tasks SET version = version + 1 WHERE task_id = ?",
                (task_id,),
            )
            return self._record(self._select_task(connection, task_id))

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
            connection.execute(
                "UPDATE tasks SET phase = ?, version = version + 1 WHERE task_id = ?",
                (phase.value, task_id),
            )
            return self._record(self._select_task(connection, task_id))

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
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, result_json = ?, version = version + 1
                WHERE task_id = ?
                """,
                (TaskStatus.SUCCEEDED.value, finished_at, result.model_dump_json(), task_id),
            )
            connection.execute("DELETE FROM worker_lease WHERE singleton = ?", (1,))
            return self._record(self._select_task(connection, task_id))

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
            connection.execute("DELETE FROM worker_lease WHERE singleton = ?", (1,))
            return self._record(self._select_task(connection, task_id))

    def recover_expired(self, *, now: datetime) -> list[str]:
        """Interrupt the running task whose singleton lease has expired."""
        now_text = _timestamp(now)
        with self._write() as connection:
            lease = connection.execute("SELECT * FROM worker_lease WHERE singleton = ?", (1,)).fetchone()
            if lease is None or lease["expires_at"] > now_text:
                return []
            task_id = str(lease["task_id"])
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
                recovered.append(task_id)
            connection.execute("DELETE FROM worker_lease WHERE singleton = ?", (1,))
            return recovered

    def _require_running_owner(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        worker_id: str,
        *,
        now: datetime,
    ) -> None:
        row = self._select_task(connection, task_id)
        if TaskStatus(row["status"]) is not TaskStatus.RUNNING:
            raise TaskConflict("Task is not running")
        lease = connection.execute("SELECT * FROM worker_lease WHERE singleton = ?", (1,)).fetchone()
        if (
            lease is None
            or lease["task_id"] != task_id
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
    def _record(row: sqlite3.Row) -> TaskRecord:
        task_id = row["task_id"]
        if not isinstance(task_id, str):
            raise TypeError("Stored task ID is not text")
        EvaluationPaths(Path("."), task_id)
        request = TaskCreate.model_validate_json(row["request_json"], strict=True)
        result = None
        if row["result_json"] is not None:
            result = TaskResult.model_validate_json(row["result_json"], strict=True)
        failure = None
        if (
            row["failure_category"] is not None
            or row["failure_phase"] is not None
            or row["failure_summary"] is not None
        ):
            failure = SafeFailure.model_validate(
                {
                    "category": (
                        FailureCategory(row["failure_category"]) if row["failure_category"] is not None else None
                    ),
                    "phase": TaskPhase(row["failure_phase"]) if row["failure_phase"] is not None else None,
                    "summary": row["failure_summary"],
                },
                strict=True,
            )
        return TaskRecord.model_validate(
            {
                "task_id": task_id,
                "request": request,
                "status": TaskStatus(row["status"]),
                "phase": TaskPhase(row["phase"]) if row["phase"] is not None else None,
                "created_at": _parse_timestamp(row["created_at"]),
                "started_at": _parse_optional_timestamp(row["started_at"]),
                "finished_at": _parse_optional_timestamp(row["finished_at"]),
                "version": row["version"],
                "failure_category": failure.category if failure is not None else None,
                "failure_phase": failure.phase if failure is not None else None,
                "failure_summary": failure.summary if failure is not None else None,
                "result": result,
            },
            strict=True,
        )

    @staticmethod
    def _summary(record: TaskRecord) -> TaskSummary:
        result = record.result
        return TaskSummary(
            task_id=record.task_id,
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


def _task_id(now: datetime, sequence: int) -> str:
    _timestamp(now)
    task_id = f"run-{now.astimezone(UTC):%Y%m%d-%H%M%S-%f}-{sequence:010d}-{uuid.uuid4().hex[:8]}"
    EvaluationPaths(Path("."), task_id)
    return task_id
