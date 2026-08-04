# Codex Capacity Auto-Retry Design

## Goal

A transient upstream "model at capacity" response must not pause a running batch. The worker
absorbs it as a business-layer retry by queueing the next attempt, and only falls back to the
existing fail-closed batch pause once the retry budget is exhausted. When that fallback does fire,
the console must name the cause and say whether a retry is worth trying, instead of reporting an
undifferentiated "基础设施失败".

## Problem

Batch `batch-20260802-174300-865647-0000000004-2931f9cb` (gpt-5.6-luna, 731 tasks) paused twice
with `pause_reason = infrastructure_failure`:

| Control event | Task | Usage at the time |
| --- | --- | --- |
| seq 200, `2026-08-03T09:10:21Z` | `t0187` | ~66% |
| seq 206, `2026-08-03T14:34:52Z` | `t0310` | ~87% |

Both times the retained event stream (`runs/<run_id>/arms/off/codex/events.jsonl`) ends with:

```json
{"type":"error","message":"Selected model is at capacity. Please try a different model."}
{"type":"turn.failed","error":{"message":"Selected model is at capacity. Please try a different model."}}
```

Codex then exits non-zero. `CodexRunner.run` classifies any non-zero exit through
`_command_error_kind` (`codex.py:207`), which only inspects the return code, so the failure reaches
`_safe_failure` (`worker.py:581`) as a generic `CodexInfrastructureError`, becomes
`FailureCategory.CODEX_EXECUTION`, and `TaskStore.fail` calls
`_pause_batch_for_infrastructure_failure` unconditionally. One transient task failure therefore
stops the remaining 417 tasks until a human retries the task and resumes the batch.

## Root cause: two unrelated capacity limits

`Selected model is at capacity` is **server-side saturation of the shared model pool**. It is
independent of the account quota that drives `usage_pause_percent`. Three observations rule out the
quota gate:

- `t0187` failed at ~66% usage while the threshold was 80%.
- `t0187` attempt-0002 started at `2026-08-03T12:25:46Z`, when usage was *higher*, and succeeded.
- Only 2 of 731 tasks were affected, at unrelated times. Quota exhaustion is monotonic.

The usage probe also reported `rate_limit_reached_type: null` throughout.

The upstream advice ("try a different model") does not apply here: the batch is pinned to
gpt-5.6-luna so it stays comparable with the gpt-5.6-sol arm. Retrying the same model is the only
valid recovery, which matches both manual recoveries.

## Non-goals

- Switching models on capacity errors. It would invalidate the off/on comparison.
- Broadening auto-retry to other failure categories. Only the capacity signature is in scope.
- Explicit retry backoff. See "Retry budget" for why the natural spacing is sufficient.
- Any database migration or new API route. Two response enums widen by one value each; the frontend
  is updated in the same change so its strict schemas keep validating.
- Deleting `work/<run_id>` scratch directories, including the 1.6 TB already accumulated. Tracked
  separately; see "Retention".

## Design

Retrying must create a **new attempt**, not re-run in place: the failed session already dirtied the
arm workspace, and both `worker.py:159` and `runner.py:171` refuse to reuse an existing
`runs/<run_id>` or `work/<run_id>`. Attempt N+1 maps to `runs/<task_id>-attempt-000N/` via
`_execution_run_id` (`worker.py:541`), which gives clean isolation and a complete audit trail. This
is the same path a human retry takes.

Seven changes:

**1. `codex.py` — recognise the signature**

Add `CodexCapacityError(CodexInfrastructureError)`. In the `except CommandError` branch, after
`_retain_process_result` has persisted the artifacts, seek the event stream back to 0 and scan it:
if any event whose `type` is `error` or `turn.failed` carries a message containing `at capacity`,
raise `CodexCapacityError`; otherwise raise `CodexInfrastructureError` as today. Restricting the
scan to those two event types prevents an `agent_message` that merely quotes the phrase from
matching. Malformed lines are skipped, never raised.

**2. `web/models.py` — name the cause, carry the decision**

Add `FailureCategory.CODEX_CAPACITY = "codex_capacity_failure"` and include it in
`RETRYABLE_FAILURES`. `SafeFailure` gains `auto_retry: bool = False`.

A dedicated category is what makes the console useful: `codex_execution_failure` / "Codex 执行失败"
gives no way to tell which dependency broke or whether retrying is worthwhile. That ambiguity is why
diagnosing the two incidents required reading `events.jsonl` on the host.

**3. `web/batches.py` + `web/controls.py` — name the pause**

Add pause reason `codex_capacity` alongside `infrastructure_failure` (`batches.py:99`,
`controls.py:38`). `_pause_batch_for_infrastructure_failure` takes the reason to record, so a
budget-exhausted capacity failure pauses with `codex_capacity` while every other category keeps
recording `infrastructure_failure`.

**4. `web/worker.py` — decide, including the budget**

`_safe_failure` gains an `auto_retry_allowed: bool` parameter and a branch for
`CodexCapacityError` placed **before** the existing `CodexInfrastructureError` branch, since it is a
subclass. The call site in `_run_claimed` passes
`task.attempt_number <= self._config.codex_capacity_retry_max`.

Keeping the budget check in the worker avoids plumbing configuration into `TaskStore`.

**5. `web/store.py` — the only behavioural fork**

Extract the reusable tail of `retry_failed_task` (`store.py:887-922`: insert the next attempt, reset
the task row to `queued`, append the retry control event) into a private helper parameterised by
actor and event details.

In `fail`, after the failure rows are written, if `failure.auto_retry` is set: call that helper with
`actor="system"` and `details.reason="codex_capacity"`, and **skip**
`_pause_batch_for_infrastructure_failure`. Otherwise the path is byte-for-byte today's behaviour.

The failed attempt row is retained, so attempt history still shows what happened.

**6. `web/config.py` — one bound**

`codex_capacity_retry_max: int = 5`, read from `POWERCONTEXT_EVAL_CODEX_CAPACITY_RETRY_MAX`,
following the `_EnvironmentNumbers` pattern.

**7. Frontend — two enum values and two labels**

The console validates responses with strict `z.enum`, so an unknown value would fail validation
rather than degrade. Add `codex_capacity_failure` to `web/src/api.ts:86` and `web/src/types.ts:13`
with the label `上游模型容量不足（可重试）` in `components/TaskDetail.tsx:19`, and `codex_capacity`
to `web/src/api.ts:335` and `web/src/types.ts:262` with the label
`上游模型容量不足（自动重试耗尽）` in `components/BatchControls.tsx:33`.

This requires a frontend rebuild and redeploy. That is acceptable only because the batch is paused;
a mid-run frontend deploy previously interrupted the evaluation.

## Retry budget

Auto-retry fires while the latest attempt number is `<= codex_capacity_retry_max`, so a task reaches
at most `attempt-0006`. Manual retries share the counter, which also bounds runaway loops.

No explicit backoff. Re-running an arm takes minutes on its own (`t0310` ran 22:30:06 → 22:34:52
before failing), so retries are naturally spaced; and when the capacity error lands on the first
request the arm fails in seconds having burned almost no quota, so retrying immediately is cheap.

The budget stays finite because each failed attempt leaves roughly 3 GB of scratch in `work/`
(7 GB for `t0310`) that nothing reclaims. Unbounded retry on a fast-failing arm across ten slots
would be a disk-write storm; the quota gate cannot stop it because such failures consume almost no
tokens.

## Retention

Failed attempts keep their database row and their `runs/<run_id>/` artifacts. The cost is
negligible and the value is high: for `t0310`, `runs/` holds 224 KB while `work/` holds 7.0 GB, and
that 224 KB — specifically `arms/off/codex/events.jsonl` — is the only record of the capacity
message. Dropping it would restore exactly the blindness this change is meant to remove, and the
attempt row is what shows how many retries a task consumed.

The scratch directories are the real cost: 1.6 TB across 526 directories, 1.2 GB for a successful
task and 7.0 GB for `t0310`, none of it reclaimed. Deleting them is deliberately out of scope here
because `tokensflow_finalizations` stores `runtime_path`, `wrapper_path`, and `daemon_pid_file`
pointing inside `work/<run_id>/<arm>/runtime/`, so removal must be sequenced against the
asynchronous finalizer rather than bolted onto the failure path.

## Invariants preserved

- A paused batch is never resumed. `retry_failed_task` deliberately leaves `control_intent` alone
  (see `docs/superpowers/plans/2026-07-31-preserve-batch-pause-on-retry.md`); the helper keeps that
  property, and `fail` only ever *skips* a pause, never clears one.
- Every other failure category still pauses the batch fail-closed.
- Once the budget is exhausted, a capacity failure still pauses the batch fail-closed, drains
  in-flight tasks, and requires an explicit resume. Only the recorded `pause_reason` differs.
- Retained artifacts are never overwritten: each attempt gets its own `run_id`.
- The worker lease is released on the auto-retry path, so the requeued task is claimable.

## Testing

Existing style: `tests/web/test_worker.py` injects exceptions through the runner callable;
`tests/web/test_store.py` drives the store directly.

- `codex.py`: capacity signature → `CodexCapacityError`; plain non-zero exit → plain
  `CodexInfrastructureError`; timeout (124) unchanged; an `agent_message` quoting `at capacity` does
  not match; malformed JSONL lines are skipped.
- `worker.py`: `_safe_failure` sets `auto_retry` for `CodexCapacityError` when allowed, clears it at
  the budget, and leaves the parent class untouched.
- `store.py`: an auto-retry failure leaves the batch `run`, requeues the task, increments the
  attempt number, releases the lease, retains the failed attempt, and appends
  `task_retry_requested` with `actor="system"`; at the budget it pauses with `pause_reason`
  `codex_capacity`; every other category still pauses with `infrastructure_failure`; non-auto
  failures behave identically to today (regression guard).
- Frontend: the response schemas accept `codex_capacity_failure` and `codex_capacity`, and both
  labels render.

Commands:

```bash
uv run --project evaluation pytest evaluation/tests -q
uv run --project evaluation ruff check evaluation/src evaluation/tests
npm --prefix evaluation/web test
npm --prefix evaluation/web run build
```

## Deployment

The batch is currently `running=0 / active=0 / lease=0`, which is the only safe window: deploying
requires restarting the worker, and the worker has hung on SIGTERM and been SIGKILLed twice
(`2026-08-03 11:36`, `19:17`). Deploying now avoids interrupting ten in-flight tasks later.

Sequence: commit and push `codex/swebench-pro-eval`, pull on m0, run the Python suite and the
frontend build there so `POWERCONTEXT_EVAL_FRONTEND_DIST`
(`deploy/powercontext/evaluation/web/dist`) is regenerated, restart only `powercontext-eval-web` and
`powercontext-eval-worker`, retry `t0310`, resume Luna. `new-api`, MySQL, and Redis stay untouched.

Verification before claiming success: `GET /api/batches` shows the Luna batch back at
`control.intent = run` with no `pause_reason`, and `t0310` reports `attempt_number = 2` running.
