# Batch List Ordering and TokensFlow Finalizer Design

**Date:** 2026-08-03

**Status:** Approved; implementation authorized

**Builds on:** `2026-08-02-tokensflow-container-telemetry-design.md`

## 1. Purpose

This change fixes two independent behaviors:

1. the report index must sort batches by `created_at` descending, mark only the first sorted batch as newest, and use
   one complete Chinese label map for every batch status (`paused` is `已暂停`; `pausing` is `暂停中`);
2. TokensFlow collection after evaluation must not occupy an evaluation slot or change a completed task into an
   infrastructure failure. Once OFF/ON evidence needed by official evaluation is safely captured, the evaluation
   continues while a durable finalizer supervises the still-running TokensFlow container for at most ten minutes.

The two changes share no runtime dependency and may be implemented in parallel, then accepted together.

## 2. Scope and Non-goals

### In scope

- sort the report index by parsed `created_at` descending, with `batch_id` as a deterministic tie-breaker;
- share one exhaustive `BatchStatus -> Chinese label` map between the report index and batch overview;
- persist one TokensFlow finalization job for each handed-off OFF or ON container;
- let the Worker release the task lease and claim replacement work without waiting for finalization;
- resume pending jobs after a Worker restart;
- accept only the exact doctor line `[PASS] queue: caught up (0 pending files)`;
- gracefully stop and clean a caught-up container;
- after ten minutes, force-clean the container regardless of doctor state;
- cap finalizer-owned containers and force-reclaim the oldest jobs first when capacity is exceeded;
- expose only sanitized `pending`, `succeeded`, or `timed_out` finalization facts in task detail.

### Non-goals

- changing OFF/ON, official evaluator, report, pass-rate, Token, retry, pause, or quota semantics;
- making TokensFlow finalization part of task success, task failure, batch pause, or task-pair capacity;
- retaining raw doctor output, daemon logs, identity, credentials, configuration values, or upload payloads;
- hard-coding the selected TokensFlow binary, host home, Codex configuration, or `TOKENSFLOW_*` values;
- restarting or reconfiguring Docker, new-api, MySQL, or Redis.

## 3. Batch-list presentation

`ReportIndex` copies and sorts the API result before rendering. It never mutates the API-owned array. Ordering uses
`Date.parse(created_at)` descending and `batch_id` descending for equal timestamps. The `最新批次` marker is attached
only after sorting.

A shared `batchStatusLabels: Record<BatchStatus, string>` supplies all seven labels:

| Status | Label |
| --- | --- |
| `queued` | `排队中` |
| `running` | `进行中` |
| `pausing` | `暂停中` |
| `paused` | `已暂停` |
| `cancelling` | `取消中` |
| `completed` | `已完成` |
| `cancelled` | `已取消` |

Both the report index and batch overview consume this map; neither keeps a fallback that renders unknown nonterminal
states as `进行中`.

## 4. Evaluation-to-finalizer handoff

The existing pre-inference identity gate and daemon readiness remain required infrastructure checks. After Codex has
finished an arm, the SUT synchronously captures every artifact needed by treatment validation and later official
evaluation: patch-bearing workspace, Codex/context traces, PowerContext evidence and sanitized logs. It then disconnects
the arm container from the private task network while leaving the configured TokensFlow egress network attached.

Before returning the arm outcome, the SUT atomically registers a finalization job. Only a successful durable register
transfers ownership of the container, wrapper, private TokensFlow home and daemon handle to the finalizer. If the
register fails, the SUT retains ownership, performs bounded cleanup, and fails closed; it never leaves an untracked
container.

The runner then performs official OFF/ON evaluation and report generation from host-mounted artifacts. Once the report
is validated, `TaskStore.succeed` releases the task lease as it does today. Pending finalization jobs do not count in
`running_tasks`, `active_task_pairs`, leases, task parallelism, usage-pause decisions, or batch status, so a slot may
immediately claim the next logical task.

Standalone CLI runs without a durable job registrar keep the current synchronous cleanup path; only the supervised
Worker enables asynchronous handoff.

## 5. Durable finalization jobs

SQLite adds `tokensflow_finalizations`, uniquely keyed by `(attempt_id, arm)`. Each row stores only operational facts:
job/attempt/task identifiers, arm, run ID, container name, relative runtime/control paths, daemon PID-file path,
registration/deadline/check/finish timestamps, state, queue-caught-up boolean, doctor return code, and a sanitized
terminal reason. Paths must resolve beneath the configured run root; container/run/arm identifiers must match the
existing safe-name contracts.

The state machine is:

```text
pending -> caught_up -> succeeded
pending -> timing_out -> timed_out
```

The intermediate state is committed before cleanup. A crash after doctor PASS or after a timeout decision therefore
resumes idempotent cleanup rather than repeating the decision or losing its evidence. Container absence is accepted
only after Docker inspect proves the exact registered container is absent. Wrapper and network cleanup use the existing
safe no-follow and task-scoped checks.

The job does not persist raw `HOME`, binary paths, or `TOKENSFLOW_*` values. The handed-off container already owns the
read-only binary mount and the per-arm environment snapshot selected at launch. Finalizer commands execute the bare
`tokensflow` wrapper inside that container, inheriting its captured `HOME` and safe dynamic `TOKENSFLOW_*` environment.

## 6. Finalizer supervisor

`EvaluationWorker` starts one independent `TokensFlowFinalizer` supervisor alongside task-pair slots. It owns no task
lease and continues while a batch is running or paused. Its poll loop:

1. reads all nonterminal jobs ordered by registration time and stable job sequence;
2. if the configured finalizer-container limit is exceeded, moves the oldest excess jobs to `timing_out` with reason
   `capacity_reclaimed` and force-cleans them;
3. moves jobs at or beyond `registered_at + 600 seconds` to `timing_out` with reason `deadline` and force-cleans them;
4. for remaining `pending` jobs, runs bare `tokensflow doctor` and parses output in memory;
5. moves a job to `caught_up` only when one normalized output line equals exactly
   `[PASS] queue: caught up (0 pending files)`;
6. gracefully stops the registered daemon for `caught_up` jobs, then removes the container, egress attachment, wrapper
   and private runtime before marking `succeeded`;
7. force-removes and safely cleans `timing_out` jobs before marking `timed_out`.

One iteration is bounded; it never sleeps on one job for ten minutes. Doctor/cleanup failures update only sanitized
timestamps and return codes and are retried until terminal cleanup is proved. `stop()` prevents new checks promptly;
nonterminal rows remain durable for the next Worker process.

The finalizer-owned-container limit is validated deployment configuration, not task parallelism. m0 sets it explicitly
to cover the intended ten-lane workload. Capacity reclamation applies only to registered finalizer jobs and never to an
active evaluation container.

## 7. Result and failure semantics

Finalization is telemetry, not evaluation correctness:

- `succeeded` means the exact queue line passed and graceful cleanup completed;
- `timed_out` means deadline or capacity forced cleanup completed;
- both preserve an already succeeded evaluation task;
- neither calls `fail`, requests batch pause, consumes a task lease, changes official results, or changes report totals;
- a cleanup problem remains a nonterminal finalization job and a health/operations fact, not a task failure.

Task detail exposes per-arm state, timestamps, `queue_caught_up`, `doctor_rc`, and `timed_out_reason`. It never exposes
doctor output, paths, environment, identity hashes, credentials or daemon logs. The UI labels timeout as
`TokensFlow 收尾超时`; evaluation success remains visible independently.

## 8. Restart, resource and security guarantees

- Worker restart: database state is authoritative; the new supervisor resumes all nonterminal jobs idempotently.
- Resource cap: oldest registered finalizer containers are reclaimed first; active evaluation containers are excluded.
- Secret boundary: command runners receive the existing redaction variants, while persisted/API evidence is allowlisted.
- Dynamic configuration: each new arm snapshots the then-current configured binary/home/environment; a handed-off arm
  keeps its launch snapshot and does not consult later host changes.
- Filesystem safety: job paths are relative, validated below the run root, opened no-follow and never used as broad
  recursive-delete roots.

## 9. Acceptance matrix

| Scenario | Required result |
| --- | --- |
| Unsorted API list | UI renders strict `created_at` descending order and marks only the newest row |
| Paused Sol batch | Report index and overview show `已暂停`; `pausing` shows `暂停中` |
| Successful handoff | Arm artifacts are complete, job is durable, task continues, and the next task can be claimed |
| Exact queue PASS | Only `[PASS] queue: caught up (0 pending files)` enters graceful cleanup |
| WARN/near match | Similar text, pending count, different prefix or extra content stays pending |
| Ten-minute deadline | Container is force-cleaned, job is `timed_out`, task success and batch intent are unchanged |
| Capacity pressure | Oldest finalizer job is force-cleaned; active evaluation containers are untouched |
| Worker restart | Pending/caught-up/timing-out jobs resume and reach one terminal state |
| Cleanup retry | Partial cleanup resumes idempotently without losing the terminal decision |
| Dynamic config | New arms use current configured sources; handed-off arms keep their launch environment |
| Secret scan | No raw doctor output, identity, configuration or credential enters DB/API/log/report evidence |

## 10. Deployment boundary

Development, frontend tests, backend tests and builds may run while evaluation continues. Production activation waits
until `running_tasks=0`, `active_task_pairs=0`, and leases are zero. At that boundary both relevant batches remain
paused; only evaluation Web/Worker services required by the changed code are restarted. Docker, new-api, MySQL and Redis
are not restarted or reconfigured.

After activation, one controlled task proves OFF/ON handoff, next-task admission, exact queue PASS, graceful cleanup,
task-detail telemetry and zero residual resources. A synthetic short-deadline test proves timeout without changing task
or batch success. Only then is the approved Luna parallelism restored and Luna explicitly resumed; Sol stays paused.
