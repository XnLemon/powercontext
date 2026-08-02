"""Synchronized account-wide usage gating for evaluation task claims."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from powercontext_eval.web.config import WebConfig
from powercontext_eval.web.controls import BatchPauseReason
from powercontext_eval.web.models import TaskRecord
from powercontext_eval.web.store import TaskStore
from powercontext_eval.web.usage import UsageSnapshot, UsageUnavailable, is_fresh


class UsageProbe(Protocol):
    def read(self, *, now: datetime) -> UsageSnapshot: ...


class ClaimCoordinator:
    """Serialize account-wide usage decisions and capacity-aware claims."""

    def __init__(
        self,
        config: WebConfig,
        store: TaskStore,
        *,
        usage_probe: UsageProbe,
        clock: Callable[[], datetime],
    ) -> None:
        self._config = config
        self._store = store
        self._usage_probe = usage_probe
        self._clock = clock
        self._lock = threading.Lock()
        self._claim_commit_lock = threading.Lock()
        self._stopped = threading.Event()

    def stop(self) -> None:
        """Close the synchronized claim gate for all sharing slots."""

        with self._claim_commit_lock:
            self._stopped.set()

    def claim(self, worker_id: str) -> TaskRecord | None:
        """Claim one task after one synchronized, fail-closed usage decision."""

        if self._stopped.is_set():
            return None
        with self._lock:
            if self._stopped.is_set():
                return None
            now = self._clock()
            self._store.recover_expired(now=now)
            try:
                snapshot = self._usage_before_claim(now)
            except UsageUnavailable:
                if self._stopped.is_set():
                    return None
                self._store.pause_runnable_batches(
                    reason=BatchPauseReason.USAGE_UNAVAILABLE,
                    now=now,
                )
                return None
            if self._stopped.is_set():
                return None
            with self._claim_commit_lock:
                if self._stopped.is_set():
                    return None
                return self._store.claim_next_with_usage(
                    worker_id,
                    snapshot=snapshot,
                    default_threshold=self._config.usage_pause_percent,
                    max_concurrency=self._config.task_parallelism,
                    now=now,
                )

    def refresh_after_attempt(self, batch_id: str) -> None:
        """Refresh account usage and finalize batch control after one attempt."""

        with self._lock:
            now = self._clock()
            try:
                snapshot = self._usage_probe.read(now=now)
                self._store.apply_usage_snapshot(snapshot, now=now)
            except UsageUnavailable:
                self._store.pause_runnable_batches(
                    reason=BatchPauseReason.USAGE_UNAVAILABLE,
                    now=now,
                )
            self._store.finalize_batch_intent_after_attempt(batch_id, now=now)

    def _usage_before_claim(self, now: datetime) -> UsageSnapshot:
        snapshot = self._store.latest_usage_snapshot()
        if snapshot is not None and is_fresh(
            snapshot,
            now=now,
            max_age=timedelta(seconds=self._config.usage_probe_seconds),
        ):
            return snapshot
        return self._usage_probe.read(now=now)
