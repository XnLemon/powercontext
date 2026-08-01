# Evaluation Task-Pair Parallelism Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run at most four isolated SWE-bench Pro OFF/ON task pairs concurrently while preserving safe pause, usage, failure, recovery, and reporting behavior.

**Architecture:** Keep one evaluation Worker service as the process owner. A supervisor runs configurable task-pair slots, while a synchronized claim coordinator serializes account-usage checks and atomic SQLite claims; per-attempt leases enforce capacity and independent recovery.

**Tech Stack:** Python 3.11+, SQLite WAL, Pydantic, Typer, pytest, React 19, TypeScript, Zod, Vitest, systemd, Docker-backed SWE-bench Pro runner.

---

## File Responsibility Map

- `evaluation/src/powercontext_eval/web/config.py`: validate and expose the deployment parallelism setting.
- `evaluation/src/powercontext_eval/web/store.py`: migrate leases, enforce claim capacity, validate ownership, recover
  individual leases, atomically pause a batch on infrastructure failure, and report Worker runtime capacity plus
  active lease count.
- `evaluation/src/powercontext_eval/web/controls.py`: define the stable infrastructure-failure pause reason.
- `evaluation/src/powercontext_eval/web/batches.py`: define the stable infrastructure-failure control event.
- `evaluation/src/powercontext_eval/web/claiming.py`: coordinate one account-wide usage gate and capacity-aware claims.
- `evaluation/src/powercontext_eval/web/worker.py`: execute one complete task pair per slot and supervise the configured
  slots under one process-owner lock.
- `evaluation/src/powercontext_eval/web/models.py`: extend the health response contract.
- `evaluation/src/powercontext_eval/web/api.py`: combine store health with configured capacity.
- `evaluation/src/powercontext_eval/cli.py`: keep the CLI entry point pointed at the Worker supervisor and update its
  truthful description.
- `evaluation/web/src/{types.ts,api.ts}`: validate the extended health contract.
- `evaluation/web/src/components/{AppShell.tsx,BatchLauncher.tsx}`: show active/capacity task pairs and remove the
  obsolete serial-only wording.
- `evaluation/deploy/powercontext-eval.env.example`: document the safe default without changing Docker or other
  services.
- `evaluation/README.md`: replace the deferred serial contract with configurable task-pair parallelism and rollout
  instructions.
- `evaluation/tests/web/test_{config,store,claiming,worker,controls,api,cli,deployment}.py`: protect backend behavior.
- `evaluation/web/src/{api,App}.test.tsx` and `evaluation/web/src/test/fixtures.ts`: protect frontend behavior.

## Required Behavior Matrix

| Slice | Scenarios protected before implementation |
| --- | --- |
| Configuration | default 1; explicit 4; reject 0, 5, and non-integer values; example environment remains valid |
| Lease migration | legacy task-ID singleton and current attempt-ID singleton both migrate without losing ownership |
| Claims | capacity 1 preserves serial behavior; capacity 4 permits exactly four; fifth denied; race never duplicates |
| Ownership | heartbeat, success, and failure touch only the caller's attempt lease |
| Recovery | only expired leases become interrupted; healthy leases continue; recovery failure pauses a batch |
| Controls | user pause/cancel stop replacements; infrastructure failure and pause persist atomically |
| Usage | concurrent slots share one probe; threshold, quota, and unavailable states stop all new claims |
| Supervisor | one process lock; four long-lived slots; graceful stop finishes active pairs and starts no replacements |
| Isolation | concurrent task configs have distinct run IDs, workspaces, homes, networks, and treatment scopes |
| Reporting | source-index and attempt-number ordering remains unchanged |
| Health/UI | configured capacity and active count are visible; no secret or worker identity is returned |
| m0 rollout | exactly four claims in the validation wave; no fifth; all four finish and clean up before sustained run |

### Task 1: Add the bounded parallelism configuration

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/config.py`
- Modify: `evaluation/tests/web/test_config.py`
- Modify: `evaluation/tests/web/test_deployment.py`
- Modify: `evaluation/deploy/powercontext-eval.env.example`

- [ ] **Step 1: Write failing configuration tests**

Add assertions for the serial default and explicit environment value:

```python
def test_web_config_defaults_to_one_task_pair(tmp_path: Path) -> None:
    assert WebConfig.for_root(tmp_path).task_parallelism == 1


def test_web_config_reads_task_parallelism(tmp_path: Path) -> None:
    config = WebConfig.from_environment(
        {
            "POWERCONTEXT_EVAL_ROOT": str(tmp_path),
            "POWERCONTEXT_EVAL_TASK_PARALLELISM": "4",
        }
    )
    assert config.task_parallelism == 4


@pytest.mark.parametrize("value", ["0", "5", "many"])
def test_web_config_rejects_invalid_task_parallelism(tmp_path: Path, value: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        WebConfig.from_environment(
            {
                "POWERCONTEXT_EVAL_ROOT": str(tmp_path),
                "POWERCONTEXT_EVAL_TASK_PARALLELISM": value,
            }
        )
```

Extend `EXPECTED_ENVIRONMENT_KEYS` and assert the example value is `1`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_config.py evaluation/tests/web/test_deployment.py -q
```

Expected: failures report that `WebConfig` has no `task_parallelism` field and the example key set differs.

- [ ] **Step 3: Implement the validated configuration field**

Add the same bounded field to the environment coercion model and immutable runtime config, thread it through
`for_root`, and read the named environment variable:

```python
task_parallelism: Annotated[int, Field(ge=1, le=4)] = 1
```

```python
"task_parallelism": environ.get(f"{prefix}TASK_PARALLELISM", "1"),
```

Add this non-secret line to the example environment:

```text
POWERCONTEXT_EVAL_TASK_PARALLELISM=1
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the command from step 2.

Expected: all configuration and deployment tests pass.

- [ ] **Step 5: Commit the configuration slice**

```bash
git add evaluation/src/powercontext_eval/web/config.py \
  evaluation/tests/web/test_config.py \
  evaluation/tests/web/test_deployment.py \
  evaluation/deploy/powercontext-eval.env.example
git commit -m "feat(eval): configure task-pair parallelism"
```

### Task 2: Migrate the singleton lease to per-attempt leases

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/store.py`
- Modify: `evaluation/tests/web/test_store.py`

- [ ] **Step 1: Write failing migration and health tests**

Protect both historical schemas and the new runtime-health contract. The post-migration lease table must be
`worker_leases(attempt_id, worker_id, expires_at)`, must retain the old owner, and the runtime table must default to
serial capacity:

```python
with sqlite3.connect(database) as connection:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    columns = [
        row[1]
        for row in connection.execute("PRAGMA table_info(worker_leases)").fetchall()
    ]
assert "worker_lease" not in tables
assert columns == ["attempt_id", "worker_id", "expires_at"]
assert store.health_snapshot(now=NOW)["active_task_pairs"] == 1
assert store.health_snapshot(now=NOW)["task_parallelism"] == 1
```

Create one fixture with `worker_lease.task_id` and another with `worker_lease.attempt_id`; initialize twice to prove
the migration is idempotent.

- [ ] **Step 2: Run migration tests and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_store.py -k "initialize or health" -q
```

Expected: failures show the plural table and `active_task_pairs` do not exist.

- [ ] **Step 3: Implement the plural schema and idempotent migration**

Replace the singleton creation with:

```sql
CREATE TABLE IF NOT EXISTS worker_leases (
    attempt_id TEXT PRIMARY KEY REFERENCES task_attempts(attempt_id),
    worker_id TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS worker_leases_expiry
    ON worker_leases(expires_at);
CREATE TABLE IF NOT EXISTS worker_runtime (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    task_parallelism INTEGER NOT NULL CHECK (task_parallelism BETWEEN 1 AND 4),
    observed_at TEXT NOT NULL
);
```

During initialization, detect `worker_lease`. If it stores `task_id`, join to the newest migrated attempt; if it
stores `attempt_id`, copy directly. Use `INSERT OR IGNORE`, then drop only the singular table in the same transaction.

Change the health snapshot shape to:

```python
class HealthSnapshot(TypedDict):
    worker_lease_active: bool
    active_task_pairs: int
    task_parallelism: int
    queued_tasks: int
    running_tasks: int
```

When the runtime row is absent, health returns capacity one. Add `record_worker_capacity(task_parallelism, now)` to
upsert it only after the supervisor owns the process lock. Count only leases whose `expires_at > now`; derive the
compatibility boolean from `active_task_pairs > 0`.

- [ ] **Step 4: Run store migration and health tests and verify GREEN**

Run the command from step 2.

Expected: both legacy migrations, idempotent initialization, and health-count tests pass.

- [ ] **Step 5: Commit the schema slice**

```bash
git add evaluation/src/powercontext_eval/web/store.py evaluation/tests/web/test_store.py
git commit -m "feat(eval): persist per-attempt worker leases"
```

### Task 3: Enforce capacity and independent lease ownership atomically

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/store.py`
- Modify: `evaluation/tests/web/test_store.py`

- [ ] **Step 1: Write failing capacity and race tests**

Create five queued tasks. Four claims with distinct workers and `max_concurrency=4` must succeed; the fifth must
remain queued. Preserve the old default-one assertion:

```python
claimed = [
    store.claim_next(f"worker-{index}", now=NOW, max_concurrency=4)
    for index in range(4)
]
assert all(task is not None for task in claimed)
assert store.claim_next("worker-5", now=NOW, max_concurrency=4) is None
assert store.health_snapshot(now=NOW)["active_task_pairs"] == 4
```

Add a `threading.Barrier` race using separate `TaskStore` connections. Assert four unique task IDs, four unique lease
owners, and one queued task. Add independent heartbeat/success tests showing one completed lease reduces the active
count from four to three without changing the other three running tasks.

- [ ] **Step 2: Run claim and ownership tests and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_store.py -k "claim or heartbeat or lease or recover" -q
```

Expected: `claim_next` rejects `max_concurrency` or the second claim is still globally excluded.

- [ ] **Step 3: Implement capacity-aware claims and per-attempt mutations**

Keep compatibility by defaulting capacity to one:

```python
def claim_next(
    self,
    worker_id: str,
    *,
    now: datetime,
    max_concurrency: int = 1,
) -> TaskRecord | None:
    if not worker_id:
        raise ValueError("worker_id must not be empty")
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or not 1 <= max_concurrency <= 4
    ):
        raise ValueError("max_concurrency must be an integer between 1 and 4")
    _timestamp(now)
    with self._write() as connection:
        return self._claim_next(
            connection,
            worker_id,
            now=now,
            allow_standalone=True,
            max_concurrency=max_concurrency,
        )
```

Thread the same keyword through `claim_next_with_usage`. Inside the existing `BEGIN IMMEDIATE` transaction:

```sql
SELECT COUNT(*)
FROM worker_leases
WHERE expires_at > ?;
```

Refuse when the result is at capacity. Insert a lease by `attempt_id`; never overwrite another lease. Update
heartbeat, success, failure, `_require_running_owner`, and recovery queries to select/delete by the current attempt ID
and worker ID.

Factor recovery into a transaction-local helper so a claim recovers expired leases before counting capacity. Return
all recovered task IDs, not only one.

- [ ] **Step 4: Run focused store tests and verify GREEN**

Run the command from step 2, then:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_store.py -q
```

Expected: all store tests pass, including the capacity-one compatibility cases.

- [ ] **Step 5: Commit the atomic lease slice**

```bash
git add evaluation/src/powercontext_eval/web/store.py evaluation/tests/web/test_store.py
git commit -m "feat(eval): claim task pairs up to capacity"
```

### Task 4: Pause atomically on infrastructure failure and expired ownership

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/controls.py`
- Modify: `evaluation/src/powercontext_eval/web/batches.py`
- Modify: `evaluation/src/powercontext_eval/web/store.py`
- Modify: `evaluation/tests/web/test_controls.py`
- Modify: `evaluation/tests/web/test_store.py`
- Modify: `evaluation/tests/web/test_worker.py`

- [ ] **Step 1: Declare failing control tests**

Add the stable public reason and event expectations:

```python
assert BatchPauseReason.INFRASTRUCTURE_FAILURE.value == "infrastructure_failure"
assert BatchControlEventType.INFRASTRUCTURE_FAILURE.value == "infrastructure_failure"
```

Create a three-child batch, claim two at capacity two, fail one, and assert in one read checkpoint:

```python
failed = store.fail(first.task_id, "worker-1", failure, now=NOW)
batch = store.get_batch(first.batch_id)
assert failed.status is TaskStatus.FAILED
assert batch.control.intent is BatchControlIntent.PAUSE
assert batch.control.pause_reason is BatchPauseReason.INFRASTRUCTURE_FAILURE
assert store.claim_next("worker-3", now=NOW, max_concurrency=2) is None
assert store.get(second.task_id).status is TaskStatus.RUNNING
```

Add the equivalent expectation for an expired batch-child lease. Replace the old Worker test that expected a later
child to run after failure: it must now expect pause and no replacement claim.

- [ ] **Step 2: Run the failure-control tests and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_controls.py \
  evaluation/tests/web/test_store.py \
  evaluation/tests/web/test_worker.py \
  -k "failure or expired or later_child or pause_reason" -q
```

Expected: the pause reason is missing and the queued replacement can still be claimed.

- [ ] **Step 3: Implement one transaction-local infrastructure pause helper**

Add:

```python
class BatchPauseReason(StrEnum):
    USER = "user"
    USAGE_THRESHOLD = "usage_threshold"
    USAGE_UNAVAILABLE = "usage_unavailable"
    QUOTA_LIMIT = "quota_limit"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
```

```python
class BatchControlEventType(StrEnum):
    BATCH_CREATED = "batch_created"
    THRESHOLD_CHANGED = "threshold_changed"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    RESUME_REQUESTED = "resume_requested"
    RESUMED = "resumed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    USAGE_THRESHOLD_REACHED = "usage_threshold_reached"
    USAGE_UNAVAILABLE = "usage_unavailable"
    QUOTA_LIMIT_REACHED = "quota_limit_reached"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    BATCH_COMPLETED = "batch_completed"
    TASK_RETRY_REQUESTED = "task_retry_requested"
```

In `TaskStore`, add a private helper that changes a runnable batch to pause, retains an existing user/cancel intent,
records the safe `failure_category`, `task_id`, and `attempt_id`, and calls `_finalize_batch_intent`. Invoke it inside
the same write transaction used by `fail` and by expired-lease recovery. Extend `_pause_event` with an explicit
infrastructure-failure branch so it cannot fall through to the quota event.

- [ ] **Step 4: Run failure-control tests and verify GREEN**

Run the command from step 2, then the complete store and Worker test files.

Expected: failure persistence, pause persistence, and no-replacement behavior pass; unrelated standalone-task failure
behavior remains unchanged.

- [ ] **Step 5: Commit the failure-boundary slice**

```bash
git add evaluation/src/powercontext_eval/web/controls.py \
  evaluation/src/powercontext_eval/web/batches.py \
  evaluation/src/powercontext_eval/web/store.py \
  evaluation/tests/web/test_controls.py \
  evaluation/tests/web/test_store.py \
  evaluation/tests/web/test_worker.py
git commit -m "fix(eval): pause batch on infrastructure failure"
```

### Task 5: Add one synchronized account-wide claim coordinator

**Files:**
- Create: `evaluation/src/powercontext_eval/web/claiming.py`
- Create: `evaluation/tests/web/test_claiming.py`
- Modify: `evaluation/src/powercontext_eval/web/worker.py`

- [ ] **Step 1: Write failing coordinator tests**

Use four threads and a counting probe. With no snapshot, only one probe should run; all four claims may reuse the
fresh persisted snapshot. Add threshold and unavailable cases that produce zero claims and a paused batch.

```python
assert probe.calls == 1
assert len([task for task in claimed if task is not None]) == 4
assert len({task.task_id for task in claimed if task is not None}) == 4
```

Also call `refresh_after_attempt(batch_id)` from four threads and assert the probe is serialized, the newest snapshot
is valid, and batch finalization remains idempotent.

- [ ] **Step 2: Run coordinator tests and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_claiming.py -q
```

Expected: collection fails because `powercontext_eval.web.claiming` does not exist.

- [ ] **Step 3: Implement `ClaimCoordinator`**

Create a focused class with one lock around snapshot selection/probing plus claim/finalization:

```python
class ClaimCoordinator:
    def claim(self, worker_id: str) -> TaskRecord | None:
        with self._lock:
            now = self._clock()
            self._store.recover_expired(now=now)
            try:
                snapshot = self._usage_before_claim(now)
            except UsageUnavailable:
                self._store.pause_runnable_batches(
                    reason=BatchPauseReason.USAGE_UNAVAILABLE,
                    now=now,
                )
                return None
            return self._store.claim_next_with_usage(
                worker_id,
                snapshot=snapshot,
                default_threshold=self._config.usage_pause_percent,
                max_concurrency=self._config.task_parallelism,
                now=now,
            )
```

`refresh_after_attempt` uses the same lock, probes once, applies the snapshot or usage-unavailable pause, then calls
`finalize_batch_intent_after_attempt`. Move the matching claim/finalization responsibilities out of the task executor.

- [ ] **Step 4: Run coordinator and existing usage/Worker tests and verify GREEN**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_claiming.py \
  evaluation/tests/web/test_usage.py \
  evaluation/tests/web/test_worker.py -q
```

Expected: one shared usage gate controls concurrent claims without changing existing freshness or fail-closed rules.

- [ ] **Step 5: Commit the claim-coordination slice**

```bash
git add evaluation/src/powercontext_eval/web/claiming.py \
  evaluation/src/powercontext_eval/web/worker.py \
  evaluation/tests/web/test_claiming.py \
  evaluation/tests/web/test_worker.py
git commit -m "feat(eval): coordinate parallel usage-gated claims"
```

### Task 6: Supervise four complete isolated task-pair slots

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/worker.py`
- Modify: `evaluation/src/powercontext_eval/cli.py`
- Modify: `evaluation/tests/web/test_worker.py`
- Modify: `evaluation/tests/web/test_cli.py`
- Verify: `evaluation/src/powercontext_eval/runner.py`
- Verify: `evaluation/src/powercontext_eval/powercontext_sut.py`
- Verify: `evaluation/tests/contract/test_codex_contract.py`

- [ ] **Step 1: Split existing Worker tests by responsibility without changing behavior**

Rename the current one-attempt execution subject to `TaskPairWorker` in tests and keep its behavioral assertions for
phase mapping, OFF/ON runner invocation, safe failures, heartbeat, artifacts, and report validation. Add supervisor
tests against `EvaluationWorker`.

Update one-attempt tests to instantiate the executor explicitly while supervisor tests retain the service class:

```python
from powercontext_eval.web.worker import EvaluationWorker, TaskPairWorker

executor = TaskPairWorker(
    config,
    store,
    coordinator=coordinator,
    runner=runner,
    worker_id="slot-1",
)
assert executor.run_once() is True

supervisor = EvaluationWorker(config, store, runner=runner)
```

- [ ] **Step 2: Write failing supervisor concurrency tests**

Queue five batch children, configure parallelism four, and use a blocking runner:

```python
assert entered.wait(timeout=2)
assert store.health_snapshot(now=datetime.now(UTC))["active_task_pairs"] == 4
assert len(store.list_tasks(status=TaskStatus.QUEUED, limit=10, offset=0)) == 1
assert len(set(run_ids)) == 4
```

Request `worker.stop()` while four runners are blocked, release them, and assert the fifth task remains queued. Start
a second supervisor against the same database and assert the process-owner lock prevents it from claiming anything.

- [ ] **Step 3: Run Worker and CLI tests and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_worker.py evaluation/tests/web/test_cli.py -q
```

Expected: only one task enters and no supervisor/slot distinction exists.

- [ ] **Step 4: Implement the supervisor and slot-local mutable dependencies**

Move the current execution path into `TaskPairWorker`. It receives a stable worker ID and the shared
`ClaimCoordinator`; production construction creates a separate source resolver, process runner, and lazy catalog per
slot.

`EvaluationWorker.run_forever` holds `_nonblocking_worker_lock(database_path)` for the complete supervisor lifetime,
starts exactly `config.task_parallelism` named slot threads, and joins them before releasing the lock:

```python
with _nonblocking_worker_lock(self._config.database_path) as locked:
    if not locked:
        return
    self._store.record_worker_capacity(
        self._config.task_parallelism,
        now=self._clock(),
    )
    threads = tuple(
        threading.Thread(
            target=slot.run_forever,
            args=(self._stop,),
            daemon=False,
            name=f"evaluation-slot-{index + 1}",
        )
        for index, slot in enumerate(self._slots)
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
```

`stop()` sets the shared event. Slot loops check it before every claim, so active pairs finish but no replacement is
started. Update the CLI docstring from serial execution to configured task-pair execution.

- [ ] **Step 5: Verify runtime isolation derives from unique run IDs**

Add or extend a contract assertion using two safe run IDs:

```python
assert first_runtime != second_runtime
assert first_network == f"powercontext-eval-{first_run_id}"
assert second_network == f"powercontext-eval-{second_run_id}"
assert first_scope == f"eval:{first_run_id}:on"
assert second_scope == f"eval:{second_run_id}:on"
```

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_worker.py \
  evaluation/tests/web/test_cli.py \
  evaluation/tests/contract/test_codex_contract.py -q
```

Expected: four slots run concurrently, each task remains OFF-then-ON, and all derived resources are task scoped.

- [ ] **Step 6: Commit the supervisor slice**

```bash
git add evaluation/src/powercontext_eval/web/worker.py \
  evaluation/src/powercontext_eval/cli.py \
  evaluation/tests/web/test_worker.py \
  evaluation/tests/web/test_cli.py \
  evaluation/tests/contract/test_codex_contract.py
git commit -m "feat(eval): supervise parallel task-pair slots"
```

### Task 7: Expose truthful parallel health in the API and console

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/models.py`
- Modify: `evaluation/src/powercontext_eval/web/api.py`
- Modify: `evaluation/tests/web/test_api.py`
- Modify: `evaluation/web/src/types.ts`
- Modify: `evaluation/web/src/api.ts`
- Modify: `evaluation/web/src/api.test.ts`
- Modify: `evaluation/web/src/test/fixtures.ts`
- Modify: `evaluation/web/src/components/AppShell.tsx`
- Modify: `evaluation/web/src/components/BatchLauncher.tsx`
- Modify: `evaluation/web/src/App.test.tsx`

- [ ] **Step 1: Write failing backend health tests**

Extend the exact response assertion:

```python
assert health.json() == {
    "service": "ok",
    "worker_lease_active": False,
    "active_task_pairs": 0,
    "task_parallelism": 1,
    "queued_tasks": 0,
    "running_tasks": 0,
}
```

Use `config.model_copy(update={"task_parallelism": 4})` plus four active leases and assert `4/4`. Continue running
the existing secret scan. Record capacity through `store.record_worker_capacity(4, now=NOW)` so this test proves Web
does not need a restart or its own environment refresh.

- [ ] **Step 2: Run backend API tests and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_api.py -k health -q
```

Expected: the response model rejects the new fields or the exact payload lacks them.

- [ ] **Step 3: Implement the backend health contract**

Extend `HealthResponse`:

```python
active_task_pairs: Annotated[int, Field(ge=0)]
task_parallelism: Annotated[int, Field(ge=1, le=4)]
```

Build it entirely from the store snapshot, which contains the capacity last published by the process-owning Worker.
Do not return worker IDs, paths, account identity, or diagnostics.

- [ ] **Step 4: Write failing frontend schema and rendering tests**

Update fixtures and expect these labels for active `3` and capacity `4`:

```typescript
expect(await screen.findByText("任务对 3 / 4")).toBeVisible();
expect(screen.getByText("队列 700")).toBeVisible();
expect(screen.queryByText("全局同时只运行一个任务，其余任务排队")).not.toBeInTheDocument();
```

Add negative schema cases for `active_task_pairs < 0`, capacity `0`, capacity `5`, and unknown fields.

- [ ] **Step 5: Run frontend tests and verify RED**

Run:

```bash
npm --prefix evaluation/web exec -- vitest run src/api.test.ts src/App.test.tsx
```

Expected: strict health parsing and rendering fail until the new fields are implemented.

- [ ] **Step 6: Implement frontend parsing and status text**

Extend the TypeScript interface and strict Zod schema with non-negative `active_task_pairs` and integer capacity 1-4.
Render:

```tsx
<span className={`health ${health.active_task_pairs > 0 ? "health--ok" : "health--idle"}`}>
  任务对 {health.active_task_pairs} / {health.task_parallelism}
</span>
```

Change the launcher contract line to `Worker 按配置并行运行独立任务对`.

- [ ] **Step 7: Run backend and complete frontend tests and verify GREEN**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_api.py -q
npm --prefix evaluation/web test
```

Expected: backend API and all Vitest tests pass.

- [ ] **Step 8: Commit the observability slice**

```bash
git add evaluation/src/powercontext_eval/web/models.py \
  evaluation/src/powercontext_eval/web/api.py \
  evaluation/tests/web/test_api.py \
  evaluation/web/src/types.ts \
  evaluation/web/src/api.ts \
  evaluation/web/src/api.test.ts \
  evaluation/web/src/test/fixtures.ts \
  evaluation/web/src/components/AppShell.tsx \
  evaluation/web/src/components/BatchLauncher.tsx \
  evaluation/web/src/App.test.tsx
git commit -m "feat(eval): show active task-pair capacity"
```

### Task 8: Update operations documentation and run complete local verification

**Files:**
- Modify: `evaluation/README.md`
- Modify: `evaluation/tests/web/test_deployment.py`

- [ ] **Step 1: Write the documentation-contract assertions**

Require the operator guide to contain:

```python
required = {
    "POWERCONTEXT_EVAL_TASK_PARALLELISM",
    "defaults to `1`",
    "four concurrent task pairs",
    "stop new claims",
    "active task pairs finish",
    "infrastructure failure",
    "active_task_pairs",
    "task_parallelism",
}
```

Also assert the obsolete fixed statement `exactly one physical OFF/ON task pair running globally` is absent.

- [ ] **Step 2: Run the deployment test and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_deployment.py -q
```

Expected: the guide still documents the deferred serial baseline.

- [ ] **Step 3: Update the operator guide**

Replace the fixed serial contract and deferred section with the implemented configuration, bounded range, safe
pause/failure semantics, health fields, controlled four-task wave, sustained-resume gate, and rollback to capacity
one. Preserve all existing credential, Docker, new-api/MySQL/Redis, Linux-source deployment, and report-acceptance
instructions.

- [ ] **Step 4: Run all local verification**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests -m "not live" -q
uv run --project evaluation ruff check evaluation
uv run --project evaluation ruff format --check evaluation
uv run --directory evaluation ty check src tests
npm --prefix evaluation/web test
git diff --check
```

Expected: every command exits zero. Record exact pytest and Vitest pass counts in the execution checkpoint.

- [ ] **Step 5: Commit the documentation and final local adjustments**

```bash
git add evaluation/README.md evaluation/tests/web/test_deployment.py
git commit -m "docs(eval): operate parallel task-pair workers"
```

### Task 9: Verify, deploy Linux source, and run the controlled m0 wave

**Files:**
- No source edits expected; operational verification and deployment only.

- [ ] **Step 1: Confirm the batch and infrastructure preconditions**

Through `ssh dev` and then `ssh rongfeng.frf@100.88.99.11`, verify:

```text
batch control_intent = pause
running tasks = 0
active evaluation containers = 0
prefetched images inspected = 731/731
Codex used_percent < 80
Web and Worker units healthy before deployment
new-api, MySQL, and Redis healthy
/data/docker free space >= 1 TB
```

Expected: every condition holds. If any condition fails, keep the batch paused and stop deployment.

- [ ] **Step 2: Push committed source and check out the exact commit on m0**

Run from the Mac worktree:

```bash
git push m0:/data/powercontext-eval/source/powercontext.git \
  HEAD:refs/heads/evaluation
```

On m0, fetch and detach the deployment working tree at the pushed SHA. Verify `git rev-parse HEAD` equals the local
SHA. Do not build or copy a Mac frontend artifact.

- [ ] **Step 3: Run the complete m0 test and Linux frontend build**

On m0:

```bash
/data/powercontext-eval/bin/uv sync --project evaluation --frozen
/data/powercontext-eval/bin/uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests -m "not live" -q
/data/powercontext-eval/bin/uv run --project evaluation ruff check evaluation
/data/powercontext-eval/bin/uv run --project evaluation ruff format --check evaluation
/data/powercontext-eval/bin/uv run --directory evaluation ty check src tests
npm --prefix evaluation/web ci
npm --prefix evaluation/web test
npm --prefix evaluation/web run build
```

Expected: every command exits zero. Record exact test counts and the deployed commit.

- [ ] **Step 4: Configure four slots and restart only the evaluation Worker**

Set this non-secret line in m0's evaluation console environment:

```text
POWERCONTEXT_EVAL_TASK_PARALLELISM=4
```

Keep the batch paused. Restart only `powercontext-eval-worker.service`; do not restart/reconfigure Docker, Web,
new-api, MySQL, or Redis. Verify `/api/health` reports `task_parallelism=4` and `active_task_pairs=0`.

- [ ] **Step 5: Execute the controlled four-task wave**

After one fresh usage and service-health check, explicitly resume the batch. Poll until exactly four distinct attempts
are running, then request pause immediately. Verify:

```text
active_task_pairs = 4
running tasks = 4
no fifth attempt claimed after pause
four distinct source indexes, run IDs, workspaces, Docker networks, and scopes
events advance independently for every attempt
```

Allow all four complete task pairs to finish naturally.

- [ ] **Step 6: Accept or roll back the wave**

Acceptance requires:

```text
batch fully paused with running = 0
all four attempts completed without new infrastructure failure
all four OFF/ON sequences and official evaluations completed
reports and task timelines load
evaluation containers and networks cleaned up
Codex usage/reset time readable and below 80 before any resume
Worker/Web and new-api/MySQL/Redis healthy
host CPU, memory, and disk remain safe
```

If accepted, explicitly resume sustained four-way execution and update the monitor to expect at most four active task
pairs. If rejected, keep paused, set capacity back to one, restart only the evaluation Worker, retain evidence, and
retry only infrastructure-failed tasks after repair.

- [ ] **Step 7: Record the deployment checkpoint**

Record the source commit, local and m0 test counts, four task IDs/source indexes, starting and ending Codex usage,
batch counts, health result, and whether sustained execution was resumed. Do not include credentials or raw auth
material.

## Completion Boundary

This plan is complete only when the four-task validation wave passes and sustained execution has been explicitly
resumed at capacity four, or when a failed wave has been safely rolled back to paused capacity one with retained
evidence. Completion of this implementation plan does not complete the separate 731-task evaluation goal; that goal
still requires all tasks and aggregate/list/detail report acceptance.
