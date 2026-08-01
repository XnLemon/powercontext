# Evaluation Task-Pair Parallelism Design

**Date:** 2026-08-01

**Status:** Written specification approved; implementation planning complete

**Builds on:** `2026-07-29-subscription-controlled-batch-execution-design.md`

## 1. Purpose

The m0 evaluation console currently executes one complete SWE-bench Pro OFF/ON task pair at a time. That serial
contract made the initial runner easier to validate, but it makes the 731-task batch too slow to complete in a useful
time window. Every benchmark task already has an independent image and deterministic task-scoped runtime paths.

This design adds configurable parallelism across independent benchmark tasks while preserving the comparison and
safety contracts inside each task. The first production setting is four concurrent task pairs.

The implementation must:

- execute at most the configured number of complete task pairs concurrently;
- keep OFF before ON, official evaluation, and report generation ordered within each task;
- isolate every task's filesystem, Codex, PowerContext, Docker, and artifact resources;
- stop new claims immediately after pause, cancel, usage, or infrastructure-failure control changes;
- allow already running task pairs to finish at the existing safe task boundary;
- retain independent leases so one expired worker slot cannot corrupt or duplicate another slot's task;
- preserve deterministic report content and ordering regardless of completion order;
- default to serial execution unless deployment configuration explicitly enables parallelism.

## 2. Scope and Non-Goals

### 2.1 In scope

- one Worker service supervising a fixed number of in-process execution slots;
- a validated `POWERCONTEXT_EVAL_TASK_PARALLELISM` setting, defaulting to `1`;
- per-attempt SQLite worker leases and atomic capacity enforcement;
- concurrent execution of up to four task pairs on m0;
- process, task, and Docker resource isolation;
- pause, cancel, account-usage, infrastructure-failure, restart, and recovery semantics under concurrency;
- health and console visibility for configured capacity and active task-pair count;
- migration of the existing singleton lease without losing an active attempt;
- a controlled four-task production validation wave before sustained parallel execution.

### 2.2 Non-goals

- parallel OFF and ON arms inside one benchmark task;
- parallelism greater than four during this change;
- distributed execution across multiple hosts;
- per-slot Codex accounts, quotas, or usage budgets;
- immediate cancellation of an active arm or task pair;
- changing SWE-bench images, Docker daemon configuration, model settings, report formulas, or task retry eligibility;
- adding a user-facing parallelism editor; the initial control is deployment configuration only;
- automatically resuming a paused batch after usage reset, service restart, failure repair, or configuration change.

## 3. Product Contract

### 3.1 Unit of concurrency

The schedulable unit remains one logical benchmark task pair:

```text
OFF -> ON -> official evaluation -> report validation -> terminal task result
```

One slot owns that entire sequence. A slot never runs OFF for one task and ON for another, and no task is split across
slots during an attempt.

### 3.2 Initial and default capacity

`POWERCONTEXT_EVAL_TASK_PARALLELISM` accepts an integer from 1 through 4.

- Package and example-deployment default: `1`.
- m0 production setting after validation: `4`.
- Invalid, missing, or out-of-range values fail configuration validation; they never silently select a larger value.
- Changing the value requires restarting only the evaluation Worker service. It does not resume a batch or alter
  queued/running attempts.

The upper bound is deliberately four for this version. Raising it requires a separate decision backed by host-load,
Docker-cleanup, official-evaluator, and Codex-throttling evidence.

### 3.3 Current batch

The existing batch `batch-20260730-084842-434672-0000000003-c91c04b4` remains paused throughout implementation and
deployment. After verification, it is resumed for one controlled four-task wave, immediately requested to pause once
four attempts are running, and allowed to reach the normal task boundary. Sustained four-way execution begins only
after that wave passes the acceptance checks in section 10.

## 4. Architecture

### 4.1 Single supervisor with four slots

The Worker remains one operating-system service and one scheduler process. `EvaluationWorker` becomes a supervisor
that owns up to `task_parallelism` long-lived slot threads. Each slot has:

- a stable, unique worker ID for the process lifetime;
- its own claim/execute loop;
- its own runner-facing mutable objects, including source resolver and process runner;
- an independent task heartbeat;
- no mutable Codex conversation or runtime state shared with another slot.

Read-only immutable dataset catalog data may be shared after safe construction. Mutable runtime helpers are slot
local unless their interface is explicitly thread-safe.

The existing filesystem lock changes purpose. The supervisor holds one non-blocking process-owner lock for its
lifetime so a second Worker service cannot start against the same database. Slot concurrency is controlled by SQLite
leases, not by holding the process lock around task execution.

This approach is preferred over four independent system services because it keeps account-wide usage probing,
shutdown, health reporting, and deployment ownership in one place. It is preferred over child-process management
because the runner already creates isolated Docker executions and the extra process lifecycle would add recovery
states without increasing task isolation.

### 4.2 Slot lifecycle

On startup the supervisor:

1. acquires the process-owner lock;
2. recovers every expired attempt lease in one bounded store operation;
3. constructs the configured number of slot-local executors;
4. starts each slot loop;
5. reports configured and active capacity through health state.

On service stop, the supervisor stops new claim attempts and asks every slot to finish its current task pair. It joins
all slot threads before releasing the process-owner lock. Existing service-stop behavior therefore remains
boundary-based rather than killing a task arm.

## 5. Durable Claims and Leases

### 5.1 Schema

The singleton `worker_lease` table is replaced by a plural lease table keyed by attempt:

```text
worker_leases
  attempt_id   primary key -> task_attempts.attempt_id
  worker_id    unique
  expires_at
```

An attempt can have at most one owner, and a slot can own at most one attempt. The migration copies an existing
singleton lease to the new table before removing the old table. It is idempotent and preserves the expiry time,
worker ID, and attempt ID.

### 5.2 Atomic claim

Every claim runs inside the store's SQLite write transaction. Before selecting a queued task, it:

1. removes or recovers expired leases according to the existing interruption contract;
2. verifies the batch is still runnable;
3. verifies the supplied account-usage snapshot allows a claim;
4. counts active, unexpired leases;
5. refuses the claim when the count is at the configured capacity;
6. selects the next queued task by durable queue order;
7. inserts its lease and marks the task and newest attempt running in the same transaction.

SQLite's serialized write transaction makes concurrent slot claims observe one capacity decision at a time. Four
simultaneous slots can claim exactly four attempts; the fifth claim cannot pass the capacity check.

### 5.3 Ownership and recovery

Heartbeat, phase, success, failure, and recovery operations locate the lease by the current attempt ID and validate
both attempt and worker ownership. Finishing one attempt deletes only its lease.

Recovery scans all expired leases. Each expired running attempt becomes interrupted independently, retains its
evidence, and releases only its own lease. Healthy slots and their attempts continue unchanged. A restart cannot
requeue or duplicate an attempt whose lease remains active.

## 6. Runtime Isolation

Every attempt already has a unique execution run ID. Parallel execution must continue deriving all mutable resources
from that ID:

- evaluation workspace and worktree;
- run artifacts and report directory;
- Codex Home and PowerContext Home;
- Codex provenance, plugin cache, and event stream;
- Docker container names, network, evaluation run ID, and cleanup scope;
- OFF and ON arm outputs.

No slot may use a process-global mutable current directory, environment mutation, conversation identifier, plugin
cache, or Docker scope. Secret-bearing input files may be read by multiple slots but are never copied into reports or
health output. Parallelism does not change the existing sanitized failure boundary.

## 7. Control and Usage Semantics

### 7.1 Pause and cancel

Pause and cancel are checked in the same transaction as every new claim.

- **Pause:** once `control_intent=pause` is committed, no slot can claim a replacement task. Already running task
  pairs finish. The batch becomes fully paused when its running count reaches zero.
- **Cancel:** no replacement task is claimed. Running task pairs finish, then all remaining queued tasks become
  cancelled under the existing batch-finalization rules.

Concurrent completion cannot resume the batch. Resume remains an explicit user action gated by fresh usage and
service health.

### 7.2 Account-wide usage

Codex usage remains one account-wide control signal. Slots do not receive independent budgets.

The supervisor provides one synchronized usage gate so multiple slots do not launch duplicate probes. Every claim
must use a valid fresh snapshot under the existing freshness rules. Multiple claims may use the same fresh snapshot;
if all four were valid at claim time, all four active pairs may finish even when their combined work later reaches the
threshold.

At or above 80 percent, on a quota-limit signal, or when usage is unavailable:

- the system atomically pauses every runnable affected batch;
- no new attempts are claimed;
- already running task pairs finish;
- reset time or a later low-usage observation does not resume the batch automatically.

### 7.3 Infrastructure failure

Any environment, gold-validation, Codex, treatment, official-evaluation, report-generation, or Worker infrastructure
failure requests batch pause in the same transaction that records the failed attempt. This prevents another slot from
filling the newly available capacity between failure persistence and pause persistence.

Other task pairs that were already running may finish. No new task replaces either the failed or completed pair after
the pause request. The failed attempt remains immutable; after evidence-based repair, only that logical task is
retried, and the batch still requires an explicit resume.

## 8. Ordering and Reporting

Parallel completion order has no experimental meaning. Batch and task APIs, aggregate report generation, task-list
pagination, and exports continue ordering logical tasks by `source_index` and attempts by `attempt_number`.

Aggregate OFF/ON pass counts, rates, Token totals, durations, positive flips, and negative flips are computed from the
newest accepted attempt for each logical task exactly as before. Parallelism adds no report field that changes the
experimental verdict.

Timeline events retain their actual timestamps and per-attempt event sequence. The UI does not fabricate a global
execution order across concurrent attempts.

## 9. Observability

Worker health exposes non-secret fields:

- `task_parallelism`: configured slot capacity;
- `active_task_pairs`: current count of active, unexpired attempt leases;
- existing Worker freshness and health fields.

After acquiring the process-owner lock, the Worker writes its validated capacity to a singleton SQLite runtime-state
row. The Web service reads capacity and active leases from SQLite for every health response. Changing capacity and
restarting only the Worker therefore updates the console without restarting Web; a new Worker overwrites the previous
runtime-state value after it owns the process lock.

The console shows these values in operational status so an operator can distinguish configured capacity from actual
activity. It does not expose worker IDs, host paths, credentials, raw Codex diagnostics, or account identity.

Operational monitoring for m0 continues checking batch counts and phases, event progress, Codex used percent/reset
time, Worker/Web health, active evaluation containers, and new-api/MySQL/Redis health. With parallelism enabled, the
monitor validates that active task pairs and evaluation scopes never exceed four.

## 10. Test and Acceptance Matrix

The implementation uses behavior-first tests and grouped vertical slices. The required matrix is:

| Scenario | Expected observable behavior |
| --- | --- |
| Default configuration | Parallelism is `1`; one running attempt blocks a second claim exactly as today |
| Capacity four | Four concurrent claims succeed; a fifth claim returns no task until capacity is released |
| Claim race | Concurrent claim calls cannot exceed four leases or assign one attempt twice |
| Task isolation | Four attempts receive distinct workspace, run ID, Codex Home, PowerContext Home, Docker network, and scope |
| In-task order | Every task records OFF before ON, then official evaluation and report completion |
| User pause | Pause prevents replacements; all active task pairs finish; batch reaches paused with zero running |
| Cancel | Cancel prevents replacements; active pairs finish; remaining queued tasks become cancelled |
| Usage threshold/unavailable | Account-wide gate pauses claims; active task pairs finish; no automatic resume occurs |
| Infrastructure failure | Failure and pause persist atomically; other active pairs finish; no replacement is claimed |
| Independent heartbeat | Renewing or finishing one lease does not alter another lease |
| Expired recovery | Only expired attempts become interrupted; active attempts are neither duplicated nor recovered |
| Worker restart | Process lock prevents two supervisors; restart recovers expired ownership without duplicate execution |
| Report determinism | List, detail, aggregate, and export ordering remains based on source index and attempt number |
| Health visibility | Health reports configured capacity and current active pair count without sensitive data |

Validation includes focused store, Worker, configuration, control, reporting, deployment, and browser acceptance tests,
followed by the full non-live evaluation suite, Ruff, formatting, and type checking locally and on m0.

## 11. m0 Deployment and Controlled Rollout

The change is committed and pushed as source. m0 fetches and checks out the exact Linux source commit, then runs the
full non-live test, lint, formatting, and type-check commands. No deployable artifact is built on the Mac.

Deployment does not restart or reconfigure Docker and does not restart Web, new-api, MySQL, or Redis. Only the
evaluation Worker is restarted after the batch is confirmed fully paused with no running evaluation container.

The first production validation is one four-task wave:

1. confirm 731 images remain present, Codex usage is below 80 percent, and all services are healthy;
2. set m0 parallelism to four and start the Worker while the batch remains paused;
3. explicitly resume the batch;
4. after exactly four task pairs are claimed, request pause;
5. confirm no fifth task is claimed and allow all four active task pairs to finish;
6. verify four independent scopes, continuous events, cleanup, official evaluation, reports, host load, and absence of
   new infrastructure failures or Codex throttling;
7. confirm the batch is fully paused with zero running tasks;
8. only after those checks, explicitly resume sustained four-way execution.

If the validation fails, keep the batch paused, retain all attempts and evidence, restore
`POWERCONTEXT_EVAL_TASK_PARALLELISM=1`, restart only the evaluation Worker, and diagnose before retrying only failed
tasks. Configuration rollback does not require a database downgrade because the plural lease model supports capacity
one.

## 12. Documentation Changes

Implementation updates the evaluation operations README to replace the deferred serial-baseline TODO with the new
configurable contract, document the environment variable and four-way rollout, and keep the existing safety rules for
pause, usage, retry, deployment, and service boundaries.
