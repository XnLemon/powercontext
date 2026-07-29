# Subscription-Controlled Batch Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add preview-and-confirm batch launch, configurable Codex subscription usage protection, boundary-based
pause/resume/cancel, honest token/time estimates, and immutable failed-task retries to the existing 731-task
PowerContext evaluation console.

**Architecture:** Keep the Web process free of Codex credentials and make the Worker the sole owner of a bounded
Codex App Server usage probe. Persist normalized usage snapshots, batch control intent, ordered control events, and
task attempts in SQLite; derive all API and report state from those durable facts. Preserve the existing global
single-task lease and run every new attempt through the same usage gate before model execution.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLite, pytest, React 19, TypeScript, Zod, Vitest,
Testing Library, Playwright, systemd, Codex App Server JSON-RPC.

---

## File Structure

### New backend files

- `evaluation/src/powercontext_eval/web/usage.py` — normalized Codex subscription usage types, App Server protocol
  parser, bounded probe, and freshness rules.
- `evaluation/src/powercontext_eval/web/controls.py` — batch control intent, pause reasons, control events, threshold
  validation, and visible lifecycle derivation.
- `evaluation/src/powercontext_eval/web/estimation.py` — compatible-sample selection and honest token/duration
  estimates.
- `evaluation/tests/web/test_usage.py` — App Server protocol, redaction, timeout, bucket selection, and freshness
  tests.
- `evaluation/tests/web/test_controls.py` — pure control-state and transition tests.
- `evaluation/tests/web/test_estimation.py` — estimate basis, sample labels, and unavailable-state tests.

### New frontend files

- `evaluation/web/src/components/BatchLauncher.tsx` — configure, preview, confirm, and launch flow.
- `evaluation/web/src/components/BatchLauncher.test.tsx` — non-mutating preview, threshold editing, and confirmation
  tests.
- `evaluation/web/src/components/BatchControls.tsx` — usage card, control buttons, threshold editing, and event
  timeline.
- `evaluation/web/src/components/BatchControls.test.tsx` — state-specific control behavior.
- `evaluation/web/src/components/AttemptHistory.tsx` — immutable attempt selector and retry action.
- `evaluation/web/src/components/AttemptHistory.test.tsx` — retryability and attempt navigation tests.

### Existing files modified

- `evaluation/src/powercontext_eval/web/config.py` — deployment defaults for usage threshold, probe timeout, polling,
  and freshness.
- `evaluation/src/powercontext_eval/web/batches.py` — preview/control/attempt public contracts and lifecycle values.
- `evaluation/src/powercontext_eval/web/models.py` — attempt-aware task records and retryability.
- `evaluation/src/powercontext_eval/web/store.py` — schema migration, snapshots, intents, events, attempts, claims,
  transitions, and retries.
- `evaluation/src/powercontext_eval/web/worker.py` — usage probing, safe claim gate, boundary transitions, and attempt
  execution.
- `evaluation/src/powercontext_eval/web/reporting.py` — attempt-aware aggregates, estimates, and detail responses.
- `evaluation/src/powercontext_eval/web/api.py` — preview, usage, controls, events, retry, and compatible existing
  routes.
- `evaluation/src/powercontext_eval/cli.py` — construct the usage probe for Worker only.
- `evaluation/deploy/powercontext-eval.env.example` — documented 80 percent default and probe timings.
- `evaluation/README.md` — operator workflow and non-executing deployment validation.
- `evaluation/tests/web/test_config.py`, `test_store.py`, `test_worker.py`, `test_reporting.py`, `test_api.py`,
  `test_cli.py`, `test_deployment.py`, and `fake_runner_app.py` — behavioral coverage.
- `evaluation/web/src/types.ts` and `evaluation/web/src/api.ts` — strict schemas and methods.
- `evaluation/web/src/App.tsx` — use the launcher.
- `evaluation/web/src/components/BatchOverview.tsx` — compose the control surface.
- `evaluation/web/src/components/BatchTaskReport.tsx` and `TaskRunDetail.tsx` — attempts and retry affordances.
- `evaluation/web/src/test/fixtures.ts` and affected component tests — new strict fixtures.
- `evaluation/web/src/styles.css` — desktop control and preview layouts.
- `evaluation/web/e2e/console.e2e.spec.ts` and `evaluation/tests/web/fake_runner_app.py` — complete deterministic
  control flow.

## Task 1: Define Configuration and Public Control Contracts

**Files:**
- Create: `evaluation/src/powercontext_eval/web/controls.py`
- Modify: `evaluation/src/powercontext_eval/web/config.py`
- Modify: `evaluation/src/powercontext_eval/web/batches.py`
- Modify: `evaluation/src/powercontext_eval/web/models.py`
- Test: `evaluation/tests/web/test_config.py`
- Create: `evaluation/tests/web/test_controls.py`

- [ ] **Step 1: Write failing configuration and lifecycle tests**

```python
def test_usage_control_defaults_are_safe(tmp_path: Path) -> None:
    config = WebConfig.for_root(tmp_path)
    assert config.usage_pause_percent == 80
    assert config.usage_probe_seconds == 60
    assert config.usage_probe_timeout_seconds == 15
    assert config.usage_snapshot_max_age_seconds == 120


def test_visible_batch_lifecycle_waits_for_running_task() -> None:
    assert derive_controlled_batch_status(
        intent=BatchControlIntent.PAUSE,
        task_statuses=(TaskStatus.RUNNING, TaskStatus.QUEUED),
    ) is BatchStatus.PAUSING
    assert derive_controlled_batch_status(
        intent=BatchControlIntent.PAUSE,
        task_statuses=(TaskStatus.SUCCEEDED, TaskStatus.QUEUED),
    ) is BatchStatus.PAUSED
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_config.py evaluation/tests/web/test_controls.py -q
```

Expected: collection or assertion failures because the control types and configuration fields do not exist.

- [ ] **Step 3: Add strict control types and lifecycle derivation**

```python
class BatchControlIntent(StrEnum):
    RUN = "run"
    PAUSE = "pause"
    CANCEL = "cancel"


class BatchPauseReason(StrEnum):
    USER = "user"
    USAGE_THRESHOLD = "usage_threshold"
    USAGE_UNAVAILABLE = "usage_unavailable"
    QUOTA_LIMIT = "quota_limit"


class BatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
```

`derive_controlled_batch_status()` must combine intent with the complete newest-attempt status vector and preserve
the existing all-terminal correctness.

- [ ] **Step 4: Add strict preview, threshold, control-event, and attempt contracts**

```python
class BatchPreviewRequest(_FrozenModel):
    powercontext_ref: str
    usage_pause_percent: Annotated[int, Field(ge=1, le=100)] = 80


class BatchControlPatch(_FrozenModel):
    usage_pause_percent: Annotated[int, Field(ge=1, le=100)]
    expected_version: Annotated[int, Field(ge=0)]


class TaskAttemptRecord(_FrozenModel):
    attempt_id: str
    task_id: str
    attempt_number: Annotated[int, Field(ge=1)]
    status: TaskStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
```

Add `usage_pause_percent` to `BatchCreate` so the confirmed value is immutable in the creation request while the
current editable value lives in persisted control state.

- [ ] **Step 5: Add environment parsing**

Parse:

```text
POWERCONTEXT_EVAL_USAGE_PAUSE_PERCENT=80
POWERCONTEXT_EVAL_USAGE_PROBE_SECONDS=60
POWERCONTEXT_EVAL_USAGE_PROBE_TIMEOUT_SECONDS=15
POWERCONTEXT_EVAL_USAGE_SNAPSHOT_MAX_AGE_SECONDS=120
```

Use strict bounds: threshold `1..100`, poll `10..3600`, timeout `1..60`, freshness not less than the poll interval
and not greater than `7200`.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Step 2 command.

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add evaluation/src/powercontext_eval/web/controls.py \
  evaluation/src/powercontext_eval/web/config.py \
  evaluation/src/powercontext_eval/web/batches.py \
  evaluation/src/powercontext_eval/web/models.py \
  evaluation/tests/web/test_config.py evaluation/tests/web/test_controls.py
git commit -m "feat(eval): define batch runtime controls"
```

## Task 2: Build the Bounded Codex Subscription Usage Probe

**Files:**
- Create: `evaluation/src/powercontext_eval/web/usage.py`
- Create: `evaluation/tests/web/test_usage.py`
- Modify: `evaluation/src/powercontext_eval/web/config.py`

- [ ] **Step 1: Write protocol and normalization tests**

Use a fake executable that reads JSONL and emits:

```json
{"id":0,"result":{"userAgent":"fake","codexHome":"/safe"}}
{"id":1,"result":{"rateLimits":{"limitId":"codex","primary":{"usedPercent":9,"windowDurationMins":10080,"resetsAt":1785902973}},"rateLimitsByLimitId":{"codex":{"limitId":"codex","primary":{"usedPercent":9,"windowDurationMins":10080,"resetsAt":1785902973}}}}}
{"id":2,"result":{"summary":{"lifetimeTokens":1234},"dailyUsageBuckets":[{"startDate":"2026-07-29","tokens":1234}]}}
```

Assert:

```python
snapshot = probe.read(now=NOW)
assert snapshot.limit_id == "codex"
assert snapshot.used_percent == 9
assert snapshot.remaining_percent == 91
assert snapshot.window_duration_minutes == 10_080
assert snapshot.resets_at == datetime.fromtimestamp(1785902973, UTC)
assert snapshot.observed_at == NOW
assert snapshot.account_tokens == 1234
```

Also test multi-bucket preference, backward-compatible fallback, missing `codex`, invalid percentages, timeout, output
size limit, nonzero exit, malformed JSON, missing auth file, and secret redaction.

- [ ] **Step 2: Run the usage tests and verify RED**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_usage.py -q
```

Expected: import failure for `powercontext_eval.web.usage`.

- [ ] **Step 3: Implement normalized immutable types**

```python
class UsageSnapshot(FrozenModel):
    limit_id: Literal["codex"]
    used_percent: Annotated[int, Field(ge=0, le=100)]
    remaining_percent: Annotated[int, Field(ge=0, le=100)]
    window_duration_minutes: Annotated[int, Field(ge=1)]
    resets_at: datetime
    observed_at: datetime
    rate_limit_reached_type: str | None = None
    plan_type: str | None = None
    account_tokens: Annotated[int, Field(ge=0)] | None = None
    probe_version: Literal[1] = 1
```

Define safe exceptions `UsageUnavailable` and `UsageProtocolError` whose messages never include raw server output.

- [ ] **Step 4: Implement App Server handshake and bounded request collection**

Send exactly:

```python
(
    {"method": "initialize", "id": 0, "params": {"clientInfo": CLIENT_INFO}},
    {"method": "initialized", "params": {}},
    {"method": "account/rateLimits/read", "id": 1},
    {"method": "account/usage/read", "id": 2},
)
```

Run with a temporary `CODEX_HOME` containing only a mode-0600 copy of the authorized `auth.json`, proxy variables
from validated configuration, a 15-second default timeout, and a one-megabyte output ceiling. Close stdin after
requests and terminate the full process group after the required responses.

- [ ] **Step 5: Implement bucket selection and freshness**

```python
def is_fresh(snapshot: UsageSnapshot, *, now: datetime, max_age: timedelta) -> bool:
    return timedelta(0) <= now - snapshot.observed_at <= max_age
```

Select `rateLimitsByLimitId["codex"]` first, then `rateLimits` only when `limitId == "codex"`. Ignore account email
and every nonessential field.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Step 2 command.

Expected: all usage tests pass without making a real network request.

- [ ] **Step 7: Commit**

```bash
git add evaluation/src/powercontext_eval/web/usage.py \
  evaluation/tests/web/test_usage.py evaluation/src/powercontext_eval/web/config.py
git commit -m "feat(eval): read Codex subscription usage"
```

## Task 3: Persist Usage, Control Intent, and Ordered Events

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/store.py`
- Modify: `evaluation/src/powercontext_eval/web/batches.py`
- Test: `evaluation/tests/web/test_store.py`

- [ ] **Step 1: Write migration and persistence tests**

Create a database using the pre-change schema, insert the current 731-task cancelled fixture, initialize the new
store, and assert:

```python
batch = store.get_batch(batch_id)
assert batch.control.intent is BatchControlIntent.CANCEL
assert batch.control.usage_pause_percent == 80
assert store.latest_usage_snapshot() is None
assert store.list_control_events(batch_id) == ()
```

Write focused tests for snapshot immutability, threshold optimistic concurrency, pause/cancel idempotency, event
ordering, immediate no-active-task transitions, and restart reconstruction.

- [ ] **Step 2: Run the store tests and verify RED**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_store.py -q
```

Expected: failures for missing control state and persistence methods.

- [ ] **Step 3: Add idempotent schema migration**

Add:

```sql
ALTER TABLE batches ADD COLUMN control_intent TEXT NOT NULL DEFAULT 'run';
ALTER TABLE batches ADD COLUMN usage_pause_percent INTEGER NOT NULL DEFAULT 80;
ALTER TABLE batches ADD COLUMN pause_reason TEXT;
ALTER TABLE batches ADD COLUMN control_updated_at TEXT;
ALTER TABLE batches ADD COLUMN control_version INTEGER NOT NULL DEFAULT 0;

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
```

Use column-presence checks before every `ALTER TABLE`. Convert existing all-cancelled batches to `cancel` intent
without changing child rows.

- [ ] **Step 4: Implement transactional control operations**

Provide:

```python
save_usage_snapshot(snapshot) -> UsageSnapshot
latest_usage_snapshot() -> UsageSnapshot | None
request_pause(batch_id, *, reason, now) -> BatchRecord
request_resume(batch_id, *, snapshot, now) -> BatchRecord
request_cancel(batch_id, *, now) -> BatchRecord
update_usage_threshold(batch_id, *, percent, expected_version, now) -> BatchRecord
list_control_events(batch_id) -> tuple[BatchControlEvent, ...]
```

Resume validates freshness outside the store and atomically validates `snapshot.used_percent < threshold` inside the
write transaction. Every effective transition appends one event; idempotent repeats append none.

- [ ] **Step 5: Implement boundary finalization**

```python
def finalize_batch_intent_after_attempt(self, batch_id: str, *, now: datetime) -> BatchRecord:
    with self._write() as connection:
        batch = self._select_batch(connection, batch_id)
        running = connection.execute(
            "SELECT 1 FROM tasks WHERE batch_id = ? AND status = ? LIMIT 1",
            (batch_id, TaskStatus.RUNNING.value),
        ).fetchone()
        if running is not None:
            return self._batch_record(connection, batch)
        if batch["control_intent"] == BatchControlIntent.CANCEL.value:
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
            self._append_control_event(connection, batch_id, "cancelled", "system", {}, now)
        elif batch["control_intent"] == BatchControlIntent.PAUSE.value:
            self._append_control_event_once(connection, batch_id, "paused", "system", {}, now)
        return self._batch_record(connection, self._select_batch(connection, batch_id))
```

For pause, leave queued tasks unchanged and expose `paused`. For cancel, mark every queued newest attempt cancelled
only after no attempt remains running.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Step 2 command.

Expected: all store tests pass, including migration replay.

- [ ] **Step 7: Commit**

```bash
git add evaluation/src/powercontext_eval/web/store.py \
  evaluation/src/powercontext_eval/web/batches.py evaluation/tests/web/test_store.py
git commit -m "feat(eval): persist batch control state"
```

## Task 4: Introduce Immutable Task Attempts and Retry

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/store.py`
- Modify: `evaluation/src/powercontext_eval/web/models.py`
- Modify: `evaluation/src/powercontext_eval/web/batches.py`
- Test: `evaluation/tests/web/test_store.py`
- Test: `evaluation/tests/web/test_reporting.py`

- [ ] **Step 1: Write attempt migration and retry tests**

Assert that current task rows become attempt one, that valid outcomes are not retryable, and that repeated retry
idempotency creates one attempt:

```python
attempts = store.list_task_attempts(batch_id, failed_task_id)
assert [attempt.attempt_number for attempt in attempts] == [1]

retry, created = store.retry_failed_task(
    batch_id,
    failed_task_id,
    idempotency_key="retry-0001",
    now=NOW,
)
assert created is True
assert retry.attempt_number == 2
assert store.retry_failed_task(
    batch_id,
    failed_task_id,
    idempotency_key="retry-0001",
    now=NOW,
) == (retry, False)
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_store.py evaluation/tests/web/test_reporting.py -q
```

Expected: failures for missing attempt table and retry API.

- [ ] **Step 3: Add and backfill `task_attempts`**

```sql
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
    version INTEGER NOT NULL DEFAULT 0,
    failure_category TEXT,
    failure_phase TEXT,
    failure_summary TEXT,
    result_json TEXT,
    UNIQUE(task_id, attempt_number)
);
```

Backfill one attempt per existing task in source order. Preserve the existing task ID as the logical ID and use
`f"{task_id}.attempt-0001"` as the migrated attempt ID. Update `worker_lease` to own `attempt_id`; rebuild the table
transactionally because SQLite cannot alter the foreign key in place.

- [ ] **Step 4: Make task reads newest-attempt aware**

`get`, `get_batch_task`, list, queue position, reports, and events must join the newest attempt. Compatibility
payloads still expose the logical `task_id`; new payloads add `attempt_id`, `attempt_number`, and `attempt_count`.

- [ ] **Step 5: Implement retryability and retry creation**

Retryable categories are:

```python
RETRYABLE_FAILURES = frozenset(
    {
        FailureCategory.SOURCE_RESOLUTION,
        FailureCategory.ENVIRONMENT_PREPARATION,
        FailureCategory.GOLD_VALIDATION,
        FailureCategory.CODEX_EXECUTION,
        FailureCategory.TREATMENT_VALIDATION,
        FailureCategory.OFFICIAL_EVALUATOR,
        FailureCategory.REPORT_GENERATION,
        FailureCategory.WORKER_INTERRUPTION,
        FailureCategory.INTERNAL,
    }
)
```

Require the newest attempt to be failed or interrupted. Reject succeeded tasks regardless of whether their official
result is resolved. Reopen a completed batch by setting intent to `run` and append `task_retry_requested`.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Step 2 command.

Expected: migration, reads, retryability, and report compatibility tests pass.

- [ ] **Step 7: Commit**

```bash
git add evaluation/src/powercontext_eval/web/store.py \
  evaluation/src/powercontext_eval/web/models.py \
  evaluation/src/powercontext_eval/web/batches.py \
  evaluation/tests/web/test_store.py evaluation/tests/web/test_reporting.py
git commit -m "feat(eval): retain immutable task attempts"
```

## Task 5: Enforce Usage and Control Intent in the Worker

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/worker.py`
- Modify: `evaluation/src/powercontext_eval/web/store.py`
- Modify: `evaluation/src/powercontext_eval/cli.py`
- Test: `evaluation/tests/web/test_worker.py`
- Test: `evaluation/tests/web/test_cli.py`

- [ ] **Step 1: Write worker safety tests**

Add the deterministic probe and the first boundary test:

```python
class FakeUsageProbe:
    def __init__(self, snapshots: list[UsageSnapshot]) -> None:
        self.snapshots = snapshots

    def read(self, *, now: datetime) -> UsageSnapshot:
        return self.snapshots.pop(0).model_copy(update={"observed_at": now})


def test_worker_pauses_before_claim_when_usage_reaches_threshold(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    batch = _create_batch(store, instance_ids=("instance-a",))
    calls: list[str] = []
    probe = FakeUsageProbe([usage_snapshot(used_percent=80)])
    worker = EvaluationWorker(
        config,
        store,
        usage_probe=probe,
        runner=lambda run_config, **_kwargs: calls.append(run_config.run_id),
    )

    assert worker.run_once() is False
    assert calls == []
    assert store.get_batch(batch.batch_id).status is BatchStatus.PAUSED
```

Add six more focused tests with exact final assertions:

- pause requested inside the runner leaves the current attempt succeeded and the next attempt queued;
- cancel requested inside the runner leaves the current attempt succeeded and every remaining attempt cancelled;
- a paused oldest batch is skipped and the next runnable batch is claimed;
- `UsageUnavailable` produces no runner call and a `usage_unavailable` control event;
- resume leaves all previously succeeded attempt IDs unchanged and claims the first remaining queued attempt;
- retry claims only attempt two of the selected failed task and never calls the runner for another task.

- [ ] **Step 2: Run worker tests and verify RED**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_worker.py evaluation/tests/web/test_cli.py -q
```

Expected: failures because Worker does not probe usage or honor control intent.

- [ ] **Step 3: Inject the usage probe**

Define the injected boundary and store it in the existing constructor:

```python
class UsageProbe(Protocol):
    def read(self, *, now: datetime) -> UsageSnapshot: ...


def __init__(
    self,
    config: WebConfig,
    store: TaskStore,
    *,
    usage_probe: UsageProbe,
    runner: Runner | None = None,
    worker_id: str | None = None,
) -> None:
    self._config = config
    self._store = store
    self._usage_probe = usage_probe
    self._batch_runner = runner or run_swebench_pro_instance
    self._worker_id = worker_id or f"worker-{uuid4().hex}"
```

CLI Worker construction supplies the real `CodexUsageProbe`; tests supply a deterministic fake.

- [ ] **Step 4: Add the safe claim gate**

`run_once()` must:

1. acquire the existing nonblocking host Worker lock;
2. recover expired ownership;
3. probe and persist usage;
4. transactionally pause over-threshold runnable batches;
5. claim the oldest attempt from a remaining `run` batch;
6. execute at most that one attempt.

If probing fails, persist `usage_unavailable`, request pause for runnable batches, and return without invoking the
runner.

- [ ] **Step 5: Finalize pending intent after execution**

After success or safe failure, refresh usage and finalize:

- user pause;
- usage-threshold pause;
- quota pause;
- usage-unavailable pause;
- user cancel and queued-attempt cancellation.

The runner exception mapping stays sanitized. A quota-specific Codex failure adds a quota control event but retains
the attempt's infrastructure failure.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Step 2 command.

Expected: all Worker and CLI tests pass, including global single-claim tests.

- [ ] **Step 7: Commit**

```bash
git add evaluation/src/powercontext_eval/web/worker.py \
  evaluation/src/powercontext_eval/web/store.py \
  evaluation/src/powercontext_eval/cli.py \
  evaluation/tests/web/test_worker.py evaluation/tests/web/test_cli.py
git commit -m "feat(eval): guard batch execution by usage"
```

## Task 6: Add Honest Estimates and Attempt-Aware Reporting

**Files:**
- Create: `evaluation/src/powercontext_eval/web/estimation.py`
- Create: `evaluation/tests/web/test_estimation.py`
- Modify: `evaluation/src/powercontext_eval/web/reporting.py`
- Modify: `evaluation/src/powercontext_eval/web/batches.py`
- Test: `evaluation/tests/web/test_reporting.py`

- [ ] **Step 1: Write estimate and aggregate tests**

```python
def test_no_compatible_samples_returns_unavailable() -> None:
    assert estimate_batch(samples=(), remaining_tasks=731) == BatchEstimate.unavailable()


def test_four_samples_are_preliminary_and_use_observed_pair_duration() -> None:
    estimate = estimate_batch(
        samples=(
            Sample(tokens=100, duration_seconds=10),
            Sample(tokens=200, duration_seconds=20),
            Sample(tokens=300, duration_seconds=30),
            Sample(tokens=400, duration_seconds=40),
        ),
        remaining_tasks=10,
    )
    assert estimate.quality == "preliminary"
    assert estimate.sample_size == 4
    assert estimate.remaining_tokens == 2_500
    assert estimate.remaining_duration_seconds == 250
```

Add reporting tests proving a failed attempt followed by success contributes the successful result once, while all
attempts remain listed.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_estimation.py evaluation/tests/web/test_reporting.py -q
```

Expected: missing estimator and attempt fields.

- [ ] **Step 3: Implement compatible sample selection**

Match exact task set, model, reasoning effort, treatment, runner schema, and token-metrics schema. Prefer current
batch samples; otherwise use prior compatible batch samples. Exclude legacy smoke tasks and attempts without complete
token or duration measurements.

- [ ] **Step 4: Implement estimate contracts**

```python
class EstimateQuality(StrEnum):
    UNAVAILABLE = "unavailable"
    PRELIMINARY = "preliminary"
    MEASURED = "measured"


class BatchEstimate(_FrozenModel):
    quality: EstimateQuality
    sample_size: int
    remaining_tokens: int | None
    remaining_duration_seconds: int | None
    low_tokens: int | None
    high_tokens: int | None
    low_duration_seconds: int | None
    high_duration_seconds: int | None
```

Use the observed mean for the point estimate and observed 25th/75th percentiles for the factual range. Do not
extrapolate with zero samples.

- [ ] **Step 5: Make reports attempt-aware**

Aggregate from the one valid completed attempt for a logical task; otherwise use its newest failed attempt for
failure evidence. Add attempt count, retryability, estimate, control state, latest usage, and report revision to the
strict report responses.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Step 2 command.

Expected: all estimator and reporting tests pass.

- [ ] **Step 7: Commit**

```bash
git add evaluation/src/powercontext_eval/web/estimation.py \
  evaluation/src/powercontext_eval/web/reporting.py \
  evaluation/src/powercontext_eval/web/batches.py \
  evaluation/tests/web/test_estimation.py evaluation/tests/web/test_reporting.py
git commit -m "feat(eval): estimate controlled batch runs"
```

## Task 7: Expose Preview, Usage, Controls, Events, and Retry APIs

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/api.py`
- Modify: `evaluation/src/powercontext_eval/web/batches.py`
- Test: `evaluation/tests/web/test_api.py`

- [ ] **Step 1: Write API behavior tests**

Cover exact routes:

```text
POST  /api/batches/preview
POST  /api/batches
POST  /api/batches/{batch_id}/pause
POST  /api/batches/{batch_id}/resume
POST  /api/batches/{batch_id}/cancel
PATCH /api/batches/{batch_id}/controls
POST  /api/batches/{batch_id}/tasks/{task_id}/retry
GET   /api/batches/{batch_id}/control-events
GET   /api/batches/{batch_id}/tasks/{task_id}/attempts
GET   /api/account-usage
```

Assert preview creates zero rows, create requires fresh below-threshold usage, stale usage returns
`usage_unavailable`, threshold conflict returns 409, retry is idempotent, and no response contains account email,
proxy URL, or auth path.

- [ ] **Step 2: Run API tests and verify RED**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_api.py -q
```

Expected: route 404s and strict response mismatches.

- [ ] **Step 3: Add safe error contracts**

Use stable error codes:

```text
usage_unavailable
usage_threshold_reached
batch_control_conflict
batch_control_version_conflict
task_not_retryable
attempt_not_found
```

Keep messages factual and free of raw probe or runner output.

- [ ] **Step 4: Implement preview and confirmed creation**

Preview reads the latest fresh snapshot and compatible estimate without mutation. Confirmed creation revalidates the
snapshot and persists the requested threshold. Preserve batch creation idempotency.

- [ ] **Step 5: Implement control and retry routes**

Control routes call the transactional store methods and return the updated batch. Resume and retry require a fresh
snapshot below threshold. Attempt listing is ordered ascending and task detail defaults to the newest attempt.

- [ ] **Step 6: Keep legacy APIs compatible**

Existing `/api/tasks` routes remain for migration compatibility. Existing aggregate/list/detail/context routes keep
their paths and add fields without changing the meaning of existing fields.

- [ ] **Step 7: Run tests and verify GREEN**

Run the Step 2 command.

Expected: all API tests pass.

- [ ] **Step 8: Commit**

```bash
git add evaluation/src/powercontext_eval/web/api.py \
  evaluation/src/powercontext_eval/web/batches.py evaluation/tests/web/test_api.py
git commit -m "feat(eval): expose controlled batch APIs"
```

## Task 8: Build the Preview-and-Confirm Launcher

**Files:**
- Create: `evaluation/web/src/components/BatchLauncher.tsx`
- Create: `evaluation/web/src/components/BatchLauncher.test.tsx`
- Modify: `evaluation/web/src/App.tsx`
- Modify: `evaluation/web/src/api.ts`
- Modify: `evaluation/web/src/types.ts`
- Modify: `evaluation/web/src/test/fixtures.ts`
- Modify: `evaluation/web/src/styles.css`
- Delete: `evaluation/web/src/components/TaskForm.tsx`
- Delete: `evaluation/web/src/components/TaskForm.test.tsx`

- [ ] **Step 1: Write launcher tests**

Assert:

```tsx
await user.click(screen.getByRole("button", { name: "预览评测" }));
expect(api.previewBatch).toHaveBeenCalledTimes(1);
expect(api.createBatch).not.toHaveBeenCalled();
expect(screen.getByText("731 个基准任务")).toBeVisible();
expect(screen.getByText("当前用量 9%")).toBeVisible();
expect(screen.getByLabelText("暂停阈值")).toHaveValue(80);

await user.click(screen.getByRole("button", { name: "确认并开始评测" }));
expect(api.createBatch).toHaveBeenCalledTimes(1);
```

Also test unavailable usage, at-threshold rejection, no estimate, preliminary estimate, idempotent retry after a
network error, and absence of currency words.

- [ ] **Step 2: Run component test and verify RED**

```bash
npm test -- --run src/components/BatchLauncher.test.tsx
```

Run from `evaluation/web`.

Expected: missing component and API methods.

- [ ] **Step 3: Add strict Zod/API types**

Add `UsageSnapshot`, `BatchEstimate`, `BatchPreview`, expanded `BatchStatus`, control state, attempts, and safe error
schemas. Keep parsing strict so malformed server usage cannot render as zero.

- [ ] **Step 4: Implement the two-step launcher**

The component has `configure`, `preview`, and `submitting` states. Editing revision or threshold invalidates the prior
preview. Confirmation shows fixed benchmark facts, usage, reset time, estimate quality/sample count, and no monetary
language.

- [ ] **Step 5: Replace the old form and add desktop styles**

Use `BatchLauncher` on the home page and preserve the report index beside it at desktop widths. The primary action is
`预览评测`; only the preview contains `确认并开始评测`.

- [ ] **Step 6: Run component suite and production build**

```bash
npm test -- --run
npm run build
```

Run from `evaluation/web`.

Expected: all frontend tests and the TypeScript/Vite build pass.

- [ ] **Step 7: Commit**

```bash
git add evaluation/web/src evaluation/web/package-lock.json
git commit -m "feat(eval): preview complete batch launches"
```

## Task 9: Add Overall Controls, Usage, Events, and Attempt Retry UI

**Files:**
- Create: `evaluation/web/src/components/BatchControls.tsx`
- Create: `evaluation/web/src/components/BatchControls.test.tsx`
- Create: `evaluation/web/src/components/AttemptHistory.tsx`
- Create: `evaluation/web/src/components/AttemptHistory.test.tsx`
- Modify: `evaluation/web/src/components/BatchOverview.tsx`
- Modify: `evaluation/web/src/components/BatchOverview.test.tsx`
- Modify: `evaluation/web/src/components/BatchTaskReport.tsx`
- Modify: `evaluation/web/src/components/BatchTaskReport.test.tsx`
- Modify: `evaluation/web/src/components/TaskRunDetail.tsx`
- Modify: `evaluation/web/src/components/TaskRunDetail.test.tsx`
- Modify: `evaluation/web/src/api.ts`
- Modify: `evaluation/web/src/types.ts`
- Modify: `evaluation/web/src/test/fixtures.ts`
- Modify: `evaluation/web/src/styles.css`

- [ ] **Step 1: Write state-specific control tests**

Assert:

- running shows `暂停` and `取消批次`;
- pausing shows `等待当前任务完成`;
- paused shows `继续运行`;
- cancelling shows `等待当前任务完成后取消`;
- raising threshold does not call resume;
- lowering below usage calls only threshold update and renders pending pause;
- account-wide usage is labeled `Codex 账户用量`;
- estimates show sample basis or `暂无可靠估算`.

- [ ] **Step 2: Write attempt and retry tests**

Assert retry is visible only when `retryable === true`, requires confirmation, submits one idempotency key, and keeps
attempt one selectable after attempt two exists.

- [ ] **Step 3: Run focused tests and verify RED**

```bash
npm test -- --run \
  src/components/BatchControls.test.tsx \
  src/components/AttemptHistory.test.tsx \
  src/components/BatchOverview.test.tsx \
  src/components/BatchTaskReport.test.tsx \
  src/components/TaskRunDetail.test.tsx
```

Run from `evaluation/web`.

Expected: missing controls and attempt behavior.

- [ ] **Step 4: Implement the control surface**

`BatchControls` polls batch/report/usage facts, displays current task, progress, used percent, threshold, reset and
observation times, pause reason, and estimates. Disable conflicting buttons while requests are in flight and reload
authoritative state after every mutation.

- [ ] **Step 5: Implement attempt history and retry**

Show attempt number, status, phase, timestamps, safe failure, and result availability. A retry confirmation explicitly
states that only the selected infrastructure-failed task is rerun.

- [ ] **Step 6: Integrate task list and detail**

Add attempt count and retryability without changing OFF/ON correctness filters. Attempt selection changes only detail
and context timeline endpoints; navigating back preserves the task-list filters.

- [ ] **Step 7: Run frontend suite and build**

```bash
npm test -- --run
npm run build
```

Run from `evaluation/web`.

Expected: all tests and build pass.

- [ ] **Step 8: Commit**

```bash
git add evaluation/web/src
git commit -m "feat(eval): control and retry batch runs"
```

## Task 10: Complete End-to-End, Deployment, and Regression Verification

**Files:**
- Modify: `evaluation/tests/web/fake_runner_app.py`
- Modify: `evaluation/web/e2e/console.e2e.spec.ts`
- Modify: `evaluation/tests/web/test_deployment.py`
- Modify: `evaluation/deploy/powercontext-eval.env.example`
- Modify: `evaluation/README.md`
- Modify: `docs/superpowers/specs/2026-07-29-subscription-controlled-batch-execution-design.md`

- [x] **Step 1: Extend the deterministic fake environment**

The fake usage probe exposes scripted snapshots:

```text
preview 20%
after task 1: 40%
after task 2: 81%
resume snapshot: 50%
```

The fake runner blocks task 1 long enough to request pause/cancel and makes one selected task fail once then succeed
on retry. It never calls real Codex or Docker.

- [x] **Step 2: Write the complete browser flow**

Exercise:

1. preview and confirm;
2. observe one running plus queued tasks;
3. request pause and verify the current pair finishes;
4. resume below threshold;
5. auto-pause at 81 percent;
6. raise threshold without auto-resume;
7. manually resume;
8. retry one infrastructure failure;
9. cancel while a task runs and verify remaining tasks cancel afterward;
10. inspect aggregate, filtered list, detail, attempts, and exact ON context injection.

- [x] **Step 3: Run E2E and verify RED, then implement fake support**

```bash
npm run e2e
```

Run from `evaluation/web`.

Expected before fake support: test fails at preview/controls. Expected after support: all E2E tests pass.

- [x] **Step 4: Update deployment contract tests and operator docs**

Assert the example environment includes:

```text
POWERCONTEXT_EVAL_USAGE_PAUSE_PERCENT=80
POWERCONTEXT_EVAL_USAGE_PROBE_SECONDS=60
POWERCONTEXT_EVAL_USAGE_PROBE_TIMEOUT_SECONDS=15
POWERCONTEXT_EVAL_USAGE_SNAPSHOT_MAX_AGE_SECONDS=120
```

Document preview, confirmation, boundary pause/cancel, manual resume, threshold editing, retry rules, usage
unavailability, backup, rollback, and the prohibition on starting a real batch during deployment verification.

- [x] **Step 5: Run the complete local verification matrix**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests -m "not live" -q
uv run --project evaluation ruff check evaluation
uv run --project evaluation ruff format --check evaluation
uv run --directory evaluation ty check src tests
npm test -- --run
npm run build
npm run e2e
```

Run the Python commands from repository root and npm commands from `evaluation/web`.

Expected: all tests, lint, formatting, type checks, build, and E2E pass.

Recorded result: tests, lint, formatting, changed-surface type checks, build, and E2E pass. Full `ty check src tests`
has 52 test-fixture diagnostics that reproduce unchanged at the immediately preceding `0f089a8` baseline; the design
audit records this explicitly instead of claiming a full type-check pass.

- [x] **Step 6: Perform a requirements audit**

For each of the 23 acceptance criteria in the design, record the proving test, API response, rendered UI behavior, or
deployment check. Any criterion without direct evidence remains incomplete.

- [ ] **Step 7: Commit the completed implementation**

```bash
git add evaluation docs/superpowers/specs/2026-07-29-subscription-controlled-batch-execution-design.md \
  docs/superpowers/plans/2026-07-29-subscription-controlled-batch-execution.md
git commit -m "feat(eval): operate subscription-controlled batches"
```

- [ ] **Step 8: Deploy to m0 without starting real work**

1. Back up `/data/powercontext-eval/web/tasks.sqlite3`, runtime environment, systemd units, frontend, and current SHA.
2. Stop Worker, then Web.
3. update `/data/powercontext-eval/deploy/powercontext` to the verified commit;
4. synchronize the frozen evaluation environment and build the frontend;
5. migrate by starting Web, then start Worker;
6. verify a fresh sanitized usage snapshot;
7. create only a preview and exercise controls through deterministic fixtures;
8. verify service health, database counts, PowerMem PID, and unrelated Docker containers;
9. leave no queued or running real benchmark task.

- [ ] **Step 9: Visually inspect the deployed desktop UI**

Open the m0 console through a local SSH tunnel. Verify launcher, confirmation, usage card, controls, threshold editing,
events, attempt history, and existing three-level report navigation at desktop width. Capture screenshots only from
non-running validation data.

- [ ] **Step 10: Request separate approval for a real 731-task run**

Show the deployed preview facts: current used percent, threshold, reset time, estimate basis, and immutable batch
configuration. Do not confirm the real batch until the user explicitly authorizes it.
