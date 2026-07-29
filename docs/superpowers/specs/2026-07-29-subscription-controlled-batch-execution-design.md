# PowerContext Subscription-Controlled Batch Execution Design

**Date:** 2026-07-29  
**Status:** Product direction approved; specification review pending  
**Builds on:** `2026-07-29-batch-evaluation-report-design.md`

## 1. Purpose

The current evaluation console can create a fixed 731-task SWE-bench Pro public v2 batch, queue every benchmark
instance durably, execute at most one instance at a time, and report aggregate and task-level OFF/ON results. It has
not yet run a complete real batch. Its current control surface is insufficient for safely starting and supervising a
long-running Codex subscription-backed evaluation.

This design adds a production run-control layer around that existing batch executor. It must:

- require an explicit preview and confirmation before a real batch is created;
- expose the current Codex subscription usage window without inventing monetary cost;
- stop starting new benchmark tasks when a configurable usage threshold is reached;
- support pause, resume, and cancel at a safe benchmark-task boundary;
- retain completed work and resume without rerunning completed benchmark tasks;
- estimate remaining token use and wall-clock duration only when the evidence supports an estimate;
- remain safe across Web, Worker, and host restarts;
- preserve the existing one-physical-task global concurrency limit;
- never start a quota-consuming 731-task batch during deployment verification.

## 2. Terminology

The product uses these terms consistently:

- **Evaluation batch / 评测批次:** the complete fixed set of 731 SWE-bench Pro benchmark tasks under one immutable
  PowerContext revision and Codex configuration.
- **Benchmark task / 基准任务:** one of the 731 SWE-bench Pro instances.
- **Arm execution / OFF/ON 执行:** one treatment execution inside a benchmark task. A benchmark task contains one
  OFF execution and one ON execution.
- **Attempt / 执行尝试:** one immutable execution attempt for a benchmark task. A task receives another attempt only
  after an execution or evaluation-infrastructure failure.
- **Subscription usage / 订阅用量:** the account-wide Codex quota-window usage returned by Codex App Server. It is
  not a monetary amount and is not attributable only to this evaluation batch.
- **Batch token usage / 批次 Token:** the sum of token measurements retained from this batch's OFF and ON
  executions.
- **Usage threshold / 暂停阈值:** the configurable account-wide `usedPercent` at which the system stops starting new
  benchmark tasks.

## 3. Product Decisions

### 3.1 No monetary model

The evaluation uses ChatGPT-backed Codex subscription authentication. The product therefore does not show:

- currency;
- model prices;
- estimated monetary cost;
- a monetary cost ceiling.

Subscription usage and batch tokens are separate facts. The UI must not convert either one into currency.

### 3.2 Configurable threshold

The default subscription usage threshold is 80 percent.

It is configurable at three levels:

1. deployment default through `POWERCONTEXT_EVAL_USAGE_PAUSE_PERCENT`;
2. per-batch override during preview and confirmation;
3. persisted edit while a batch is queued, running, pausing, or paused.

The accepted range is an integer from 1 through 100. Every per-batch change records the previous value, new value,
timestamp, and actor in the batch control event stream.

Raising the threshold never resumes a paused batch automatically. Lowering it to or below current observed usage
requests a pause at the current benchmark-task boundary.

### 3.3 Boundary-based pause and cancel

Pause and cancel do not forcibly terminate an active benchmark task in this version.

- **Pause:** the active benchmark task finishes both OFF and ON executions and official evaluation. The Worker then
  stops claiming new tasks from that batch.
- **Cancel:** the active benchmark task finishes. The remaining unstarted tasks are then marked cancelled, and the
  batch becomes terminal.
- When no benchmark task is active, pause takes effect immediately and cancel immediately marks every remaining
  queued task cancelled.

Immediate process termination and resumable arm-level attempts are explicitly deferred.

### 3.4 Manual resume

A batch paused by the user, a usage threshold, a quota-limit response, or an unavailable usage probe never resumes
automatically. A user must explicitly request resume. Resume is accepted only when a recent usage snapshot is
available and its observed usage is below the batch threshold.

Quota-window reset does not trigger automatic resume.

### 3.5 Failed-task retry

This version supports an explicit retry for a benchmark task whose latest attempt ended in an execution or official
evaluation-infrastructure failure.

An official `UNRESOLVED` result is a valid completed benchmark outcome, not an execution failure, and is not
retryable. Successful or ordinarily unresolved tasks cannot be rerun inside the batch to search for a more favorable
model outcome.

Retry rules:

- the original failed attempt and all of its sanitized evidence remain immutable;
- retry creates a new ordered attempt with a new run ID and artifact directory;
- only the newest attempt may be queued or running;
- every attempt uses the batch's already pinned PowerContext revision and immutable model configuration;
- the retry still passes the subscription usage gate before Codex runs;
- repeated infrastructure failures may be retried again explicitly;
- retry never reruns another completed benchmark task.

When a completed batch contains infrastructure failures, retrying one of them reopens the same batch and returns it
to `queued`. The report exposes this through a new control event and an incremented report revision.

## 4. Codex Usage Source

Codex CLI 0.145.0 on m0 has been verified with the existing ChatGPT-managed authentication to support:

- `account/rateLimits/read`;
- `account/rateLimits/updated`;
- `account/usage/read`.

The authoritative control input is the main Codex rate-limit bucket:

1. prefer `rateLimitsByLimitId["codex"]` when present;
2. otherwise use the backward-compatible `rateLimits` object when its `limitId` is `codex`;
3. reject the snapshot if neither representation contains the main Codex bucket.

The control snapshot retains:

```text
limit_id
used_percent
window_duration_minutes
resets_at
rate_limit_reached_type
plan_type (when returned)
observed_at
probe_version
```

`account/usage/read` may additionally retain account token-activity summaries for display, but it is not used as the
pause threshold because subscription consumption is not a fixed linear mapping from raw token count.

The system never exposes Codex authentication material, account email, access tokens, proxy credentials, or raw
App Server diagnostic output.

## 5. Usage Probe Architecture

### 5.1 Ownership

The Worker remains the only component that uses the Codex authentication material. The Web process reads sanitized
usage snapshots from SQLite and never reads or returns `auth.json`.

The Worker owns a `CodexUsageProbe` that:

1. starts Codex App Server over stdio;
2. performs the required `initialize` / `initialized` handshake;
3. requests account rate limits and optional token activity;
4. validates and normalizes the response;
5. redacts or discards all unrelated output;
6. terminates the temporary App Server;
7. persists one immutable sanitized snapshot.

The probe is bounded by an explicit timeout and never invokes a model turn.

### 5.2 Refresh points

The Worker refreshes usage:

- when it starts;
- periodically while the service is active;
- before the first benchmark task of a batch;
- before any resumed batch starts another benchmark task;
- after every completed benchmark task;
- after Codex reports a quota or rate-limit error.

The initial polling interval is 60 seconds and is deployment-configurable. Repeated transient failures use bounded
backoff and produce one sanitized failure state.

### 5.3 Freshness and fail-closed behavior

A snapshot is fresh for control purposes for at most two polling intervals.

If no fresh valid snapshot is available:

- preview may show the batch configuration but labels subscription usage unavailable;
- confirmation cannot create a runnable batch;
- resume is rejected;
- a queued batch is changed to paused before its next benchmark task begins;
- an already active benchmark task is allowed to finish.

Usage unavailability is never interpreted as zero percent.

## 6. Batch Preview and Confirmation

Creating a batch becomes a two-step flow.

### 6.1 Preview

The user supplies:

- `powercontext_ref`;
- usage threshold, defaulting to the deployment value.

The fixed benchmark, 731-task set, model, reasoning effort, and OFF/ON treatment remain server-owned constants.

The preview returns:

- fixed task count and configuration;
- current account-wide used and remaining percentages;
- configured pause threshold;
- quota-window duration and reset time;
- latest observation time;
- historical token and duration estimates when available;
- the sample size and basis behind every estimate;
- a clear unavailable state for any unsupported estimate.

Preview creates no batch, queue rows, run artifacts, or Codex model work.

### 6.2 Confirmation

The user explicitly confirms the preview. The create request includes the exact PowerContext ref and threshold shown
in the preview plus an idempotency key.

The server requires a fresh valid usage snapshot. The Worker independently rechecks usage before executing the first
benchmark task, closing the gap between confirmation and actual work.

If current `usedPercent` is at or above the threshold, confirmation is rejected without creating runnable work.

## 7. Estimation

### 7.1 Evidence source

Token and duration estimates use only completed batch benchmark tasks with compatible:

- benchmark and task-set revision;
- Codex model;
- reasoning effort;
- treatment mode;
- runner and metrics schema version.

Legacy single-instance smoke runs are excluded.

Within a running batch, its own completed benchmark tasks are preferred over older compatible batches.

### 7.2 Metrics

For each compatible completed benchmark task:

- task tokens are the retained OFF plus ON total tokens;
- task duration is `finished_at - started_at`, covering the paired execution and official evaluation.

The preview and running report may show:

- estimated total tokens;
- estimated remaining tokens;
- estimated total duration;
- estimated remaining duration;
- observed sample size;
- a range derived from the observed distribution.

### 7.3 Honesty rules

- With no compatible samples, display `暂无可靠估算`.
- With fewer than five samples, label the estimate `初步估算`.
- Never substitute patch size, issue length, or a fabricated constant for measured token or duration history.
- Never present an estimate as a quota guarantee.
- During execution, recalculate remaining estimates after each completed benchmark task.

## 8. Persistent Control Model

### 8.1 Batch control intent

Each batch stores a durable control intent:

```text
run
pause
cancel
```

It also stores:

```text
usage_pause_percent
pause_reason
control_updated_at
control_version
```

The existing child-task records remain the source of execution truth.

### 8.2 Visible batch lifecycle

The visible lifecycle combines the durable intent with child-task states:

```text
queued -> running -> pausing -> paused -> queued/running
                    \-> cancelling -> cancelled
             \---------------------> completed
```

Rules:

- `queued`: intent is `run`, no child is running, and runnable children remain.
- `running`: intent is `run` and one child is running.
- `pausing`: intent is `pause` and one child is still running.
- `paused`: intent is `pause` and no child is running.
- `cancelling`: intent is `cancel` and one child is still running.
- `cancelled`: intent is `cancel`, no child is running, and all remaining unstarted children are cancelled.
- `completed`: every child reached a terminal execution state without a pending control transition.

The database constraint that permits only one globally leased physical task remains unchanged.

### 8.3 Control event stream

Every control transition appends an ordered event:

```text
batch_created
threshold_changed
pause_requested
paused
resume_requested
resumed
cancel_requested
cancelled
usage_threshold_reached
usage_unavailable
quota_limit_reached
batch_completed
task_retry_requested
```

Events contain only sanitized structured fields. They provide an audit trail and drive the control timeline shown in
the report.

### 8.4 Logical tasks and attempts

The batch's 731 `tasks` rows remain stable logical benchmark-task identities. Execution lifecycle and artifacts are
represented by ordered immutable `task_attempts` rows:

```text
attempt_id
task_id
attempt_number
status
phase
created_at
started_at
finished_at
failure fields
result and artifact references
```

Attempt number starts at one and is unique within a logical task. At most one attempt for a task may be queued or
running. The logical task status is derived from its newest attempt.

Migration copies each existing task's lifecycle, result, timestamps, and artifact references into attempt one while
preserving the existing `tasks.task_id` primary key as the logical identity. Compatibility lifecycle columns remain
read-only during this version and may be removed only by a later migration. Existing report links continue to resolve
to the logical task; attempt-specific detail is additive.

The aggregate report uses:

- the one successfully completed attempt when the task eventually executes successfully;
- otherwise the newest failed attempt as the task's current infrastructure-failure evidence.

Because retry is unavailable after a valid `RESOLVED` or `UNRESOLVED` outcome, aggregate correctness cannot be
improved by rerunning valid model outcomes.

## 9. Worker Claim and Completion Rules

The Worker must not let a paused or cancelling batch block runnable work in another batch.

Task selection therefore skips queued children whose batch intent is not `run`. The global lease still ensures that
at most one physical task runs across all batches.

Before claiming a new task attempt, the Worker:

1. obtains or refreshes a valid usage snapshot and persists it;
2. transactionally changes every runnable batch whose threshold is at or below the observed `usedPercent` to
   `pause`;
3. atomically acquires the global lease and claims the oldest queued attempt from the remaining `run` batches;
4. invokes Codex only after that transaction succeeds.

The store compares each batch threshold with the same immutable usage snapshot inside the claim transaction. A task
is never claimed and then requeued merely to perform the usage check.

After an active child finishes, the Worker atomically:

1. persists the child result;
2. refreshes usage;
3. applies any pending pause or cancel intent;
4. for cancel, marks the remaining queued children cancelled;
5. appends the resulting control event;
6. releases the global lease before considering another child.

A Worker restart reconstructs behavior entirely from persisted child states, control intent, events, and usage
snapshots. It never silently clears a pause or cancel request.

## 10. API Surface

The Web API adds:

```text
POST  /api/batches/preview
POST  /api/batches
POST  /api/batches/{batch_id}/pause
POST  /api/batches/{batch_id}/resume
POST  /api/batches/{batch_id}/cancel
PATCH /api/batches/{batch_id}/controls
POST  /api/batches/{batch_id}/tasks/{task_id}/retry
GET   /api/batches/{batch_id}/control-events
GET   /api/account-usage
```

Behavior:

- preview is read-only;
- create remains idempotent;
- pause, resume, and cancel are idempotent for the same target intent;
- resume returns a safe conflict when usage is unavailable or at/above threshold;
- threshold updates use optimistic concurrency through `control_version`;
- retry is accepted only when the task belongs to the batch and its newest attempt has a retryable failure;
- terminal batches reject incompatible control changes, except that a completed batch with a retryable failure may
  accept an explicit task retry;
- responses include visible lifecycle, threshold, pause reason, and latest sanitized usage snapshot;
- existing aggregate, list, detail, and timeline APIs remain compatible.

## 11. User Interface

### 11.1 New batch flow

The home page changes from one-click execution to:

```text
Configure -> Preview -> Confirm -> Batch report
```

The confirmation view shows:

- `731 个基准任务`;
- immutable benchmark, model, reasoning, and treatment configuration;
- PowerContext revision;
- current account-wide subscription usage;
- remaining percentage;
- reset time;
- editable pause threshold;
- token and duration estimates with sample basis;
- an explicit `确认并开始评测` action.

The UI does not use currency or cost language.

### 11.2 Overall report controls

The overall report adds a control strip containing:

- visible batch lifecycle;
- completed / running / remaining task counts;
- current benchmark task;
- current account used percent;
- configured threshold;
- reset time and last refresh time;
- estimated remaining tokens and duration;
- pause, resume, and cancel actions appropriate to the current state;
- a factual pause or cancel reason.

Buttons reflect accepted control intent immediately:

- `暂停` becomes `等待当前任务完成`;
- `取消批次` becomes `等待当前任务完成后取消`;
- `继续运行` remains unavailable until a fresh below-threshold snapshot exists.

### 11.3 Details

The existing task report and single-task detail stay structurally unchanged. They additionally expose:

- batch control events in chronological order;
- whether the task was skipped or cancelled by a batch control action;
- the attempt count;
- a retry action only for a retryable failure;
- an attempt selector that preserves prior failure evidence and opens the current attempt by default.

This version does not add arm-level resume, and valid completed outcomes are never rerun.

## 12. Failure Semantics

| Condition | Behavior |
|---|---|
| Usage probe times out before starting a task | Pause batch; do not invoke Codex |
| Snapshot is missing the main Codex bucket | Pause batch as usage unavailable |
| Usage is at or above threshold | Pause before next benchmark task |
| Codex reports quota exhausted during a task | Let the invocation return, retain evidence, pause batch |
| User requests pause while a task runs | Finish current OFF/ON task, then pause |
| User requests cancel while a task runs | Finish current OFF/ON task, then cancel remaining queued tasks |
| Worker restarts during pausing/cancelling | Recover intent and complete the transition |
| Threshold is raised while paused | Stay paused until explicit resume |
| Threshold is lowered below current usage | Request boundary-based pause |
| Usage resets while paused | Stay paused until explicit resume |
| User retries an infrastructure-failed task | Create a new immutable queued attempt after the usage gate |
| User tries to retry `UNRESOLVED` | Reject; this is a valid official result |

Quota and usage failures are distinct from SWE-bench correctness. They never become `UNRESOLVED` benchmark results.

## 13. Security and Privacy

- The Web API never returns account identity or authentication material.
- The usage probe inherits the existing restricted proxy and Codex home configuration without logging secrets.
- Raw App Server stdout and stderr are sanitized and bounded before any diagnostic persistence.
- Only the normalized main-bucket fields required by the UI and control logic are stored.
- Batch control actions are unauthenticated only because the deployment remains on the existing private m0 boundary;
  this design does not authorize public exposure.
- Existing artifact redaction and context-timeline sanitation remain mandatory.

## 14. Migration and Deployment

The SQLite migration:

- adds batch control fields with `run` as the default for existing batches;
- adds usage snapshots and batch control events;
- adds logical task attempts and migrates every existing task as attempt one;
- derives legacy completed or cancelled batches without rewriting child results;
- preserves the existing 731-task cancelled deployment-validation batch;
- is idempotent and restart-safe.

Deployment verification must:

1. back up the queue database and runtime configuration;
2. stop Worker before migration;
3. migrate and start Web;
4. start Worker and verify a fresh sanitized usage snapshot;
5. create only a non-executing preview;
6. exercise pause/resume/cancel with a deterministic fake runner or an already-cancelled validation fixture;
7. verify the existing report pages;
8. verify PowerMem and unrelated Docker workloads remain unchanged.

No real Codex benchmark task and no 731-task batch is started without a separate final user confirmation after the
deployed preview displays current subscription usage.

## 15. Test Strategy

### 15.1 Contract and unit tests

- App Server handshake and normalized rate-limit schema;
- multi-bucket and backward-compatible bucket selection;
- missing, malformed, timed-out, and secret-bearing responses;
- threshold bounds and deployment default;
- freshness calculation;
- lifecycle derivation from intent plus child states;
- idempotent control actions;
- optimistic threshold updates;
- estimation compatibility and minimum-sample labels;
- retryability classification and immutable attempt ordering;
- migration from the current database schema.

### 15.2 Store and worker tests

- a pause request never claims the next task after the active child finishes;
- cancel finishes the active child and cancels remaining queued children;
- paused batches do not block runnable tasks from other batches;
- usage threshold and usage-unavailable conditions pause before Codex invocation;
- lowering the threshold requests pause and raising it does not resume;
- explicit resume requires a fresh below-threshold snapshot;
- restart preserves every control intent and event;
- only one physical task remains globally claimable;
- completed tasks are never rerun;
- retry creates only one new attempt for the selected failed logical task;
- valid `RESOLVED` and `UNRESOLVED` outcomes cannot be retried;
- repeated retry requests with the same idempotency key create one attempt.

### 15.3 API and frontend tests

- preview is non-mutating;
- confirmation fails closed on stale or over-threshold usage;
- every control action renders the correct pending and terminal wording;
- retry is visible only for infrastructure failures and prior attempts remain inspectable;
- account-wide usage and batch tokens are visibly distinct;
- no currency appears;
- estimates expose basis and unavailable/preliminary states;
- existing aggregate/list/detail navigation and filters remain correct.

### 15.4 End-to-end tests

A deterministic fake App Server and fake runner cover:

1. preview and confirm below threshold;
2. execution of multiple child tasks;
3. automatic boundary pause at threshold;
4. manual resume after a lower fresh usage snapshot;
5. manual pause during an active task;
6. cancellation during an active task;
7. process restart during pending control transitions;
8. complete report reconciliation after resume;
9. failed-task retry followed by a successful attempt without rerunning other tasks.

## 16. Acceptance Criteria

1. A real batch cannot be created from the UI without preview and explicit confirmation.
2. Preview creates no queue rows and invokes no model turn.
3. Current Codex subscription used percent, remaining percent, window, reset time, and observation time are displayed
   from a sanitized App Server snapshot.
4. Currency, price, and monetary limits are absent.
5. The deployment default threshold is 80 percent and can be overridden per batch.
6. A running or paused batch threshold can be edited with an auditable versioned event.
7. At or above threshold, no new benchmark task starts.
8. Usage unavailability fails closed for start and resume.
9. Pause and cancel allow the current benchmark task's OFF/ON pair to finish.
10. Pause starts no later child from that batch.
11. Cancel marks every remaining unstarted child cancelled after the active child finishes.
12. Resume is manual and requires fresh below-threshold usage.
13. Raising a threshold or reaching the reset time does not auto-resume.
14. Completed benchmark tasks are never rerun after pause, resume, cancel, or restart.
15. Other runnable batches are not blocked by a paused batch.
16. The global one-physical-task concurrency invariant remains true.
17. Estimates state their sample basis and never fabricate values when evidence is absent.
18. Account-wide subscription usage and batch-attributed tokens are presented separately.
19. Existing aggregate, task-list, task-detail, and context-timeline behavior remains compatible.
20. Deployment verification starts no real benchmark work and requires separate final approval before a 731-task run.
21. A failed benchmark task can be retried as a new immutable attempt.
22. A valid `RESOLVED` or `UNRESOLVED` benchmark outcome cannot be retried.
23. Retrying one task never reruns another completed task.

## 17. Acceptance Evidence Audit

This table records direct evidence, rather than treating the design checklist itself as proof.

| # | Status | Direct evidence |
| ---: | --- | --- |
| 1 | Verified locally | `BatchLauncher.test.tsx` preview/confirm test and controlled-batch browser E2E |
| 2 | Verified locally | `test_batch_preview_is_read_only_and_exposes_fixed_facts_usage_and_estimate` |
| 3 | Verified on m0 | persistent-stdio probe tests plus a sanitized live m0 snapshot; launcher and controls tests display used, remaining, window, reset, and observation facts |
| 4 | Verified locally | Launcher test rejects currency/amount wording; operator guide defines subscription-only semantics |
| 5 | Verified locally | `test_preview_request_defaults_to_eighty_percent` and deployment environment contract |
| 6 | Verified locally | `test_threshold_updates_use_optimistic_concurrency_and_do_not_auto_resume` and controls UI test |
| 7 | Verified locally | `test_worker_pauses_before_claim_when_usage_reaches_configured_threshold` and 81-percent E2E pause |
| 8 | Verified locally | stale/unavailable API tests and `test_worker_fails_closed_when_usage_is_unavailable` |
| 9 | Verified locally | store/worker boundary-pause tests and controlled-batch E2E |
| 10 | Verified locally | pause boundary tests prove no later child is claimed |
| 11 | Verified locally | cancel boundary tests and E2E verify remaining children become cancelled |
| 12 | Verified locally | fresh-snapshot resume store test and E2E manual resume below the edited threshold |
| 13 | Verified locally | threshold update store test and controls UI test |
| 14 | Verified locally | `test_restart_reuses_persisted_batch_sha_and_completed_children` |
| 15 | Verified locally | `test_worker_skips_paused_oldest_batch_and_claims_next_runnable_batch` |
| 16 | Verified locally | global store lease test and `test_only_one_child_runs_physically_across_multiple_batches` |
| 17 | Verified locally | all estimation unit tests and unavailable/preliminary launcher rendering |
| 18 | Verified locally | controls account-usage card and aggregate batch Token cards are separate components |
| 19 | Verified locally | browser E2E covers aggregate, filtered list, task detail, attempt selection, and exact ON injection |
| 20 | Pending m0 verification | deployment contract and operator tests pass; live m0 backup, service, visual, and zero-real-work checks remain |
| 21 | Verified locally | immutable retry store/API/component tests and first-failure-then-success browser E2E |
| 22 | Verified locally | `test_valid_official_outcomes_are_never_retryable` and attempt-history UI test |
| 23 | Verified locally | `test_worker_executes_only_the_new_attempt_when_a_failed_task_is_retried` and E2E retained-attempt check |

Local verification on 2026-07-30 produced 611 passing Python tests, 64 passing frontend tests, a successful production
build, two passing browser E2E scenarios, passing Ruff/format checks, and passing type checks for the changed runtime
and deterministic deployment-test surfaces. Full evaluation-project `ty check src tests` still reports 52 pre-existing
test-fixture typing diagnostics; the immediately preceding `0f089a8` baseline reports the same diagnostics, so this is
recorded as baseline debt rather than a regression or a successful full type check.
