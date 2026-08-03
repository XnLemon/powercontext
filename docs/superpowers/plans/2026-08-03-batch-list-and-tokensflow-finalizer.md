# Batch List and TokensFlow Finalizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render batches in true newest-first order with complete status labels, and move bounded TokensFlow cleanup into a durable supervisor that never blocks or changes evaluation success.

**Architecture:** The frontend owns deterministic `created_at` sorting and a shared exhaustive status-label map. The backend hands artifact-complete arm containers to a SQLite queue; an independent Worker supervisor checks the exact TokensFlow queue PASS line, gracefully cleans success, force-cleans after ten minutes or capacity pressure, and exposes sanitized telemetry without owning evaluation leases.

**Tech Stack:** React 19, TypeScript, Vitest, Python 3.11+, Pydantic, SQLite, pytest, Docker CLI through `ProcessRunner`, systemd, uv, Ruff, ty.

---

## File map and order

- Create `evaluation/web/src/batchStatus.ts`; modify `ReportIndex.tsx`, `BatchOverview.tsx` and their tests.
- Modify `tokensflow.py` and `web/store.py`; create `web/finalization.py` and focused tests.
- Modify `powercontext_sut.py`, `runner.py`, and `web/worker.py` for ownership handoff.
- Modify backend/frontend task-detail contracts for sanitized finalization state.
- Modify `web/config.py`, deployment environment example and README for validated finalizer settings.

Task 1 and Task 2 touch no common files and may run in parallel. Tasks 3–5 consume Task 2 sequentially. Task 6 is the
integration and deployment gate.

### Task 1: Newest-first batches and complete status labels

**Files:**
- Create: `evaluation/web/src/batchStatus.ts`
- Modify: `evaluation/web/src/components/ReportIndex.tsx`
- Modify: `evaluation/web/src/components/ReportIndex.test.tsx`
- Modify: `evaluation/web/src/components/BatchOverview.tsx`
- Modify: `evaluation/web/src/components/BatchOverview.test.tsx`

- [ ] **Step 1: Write RED tests**

Return old/new/middle batches from the API in that order. Assert DOM order is new/middle/old, only new owns
`最新批次`, and table-drive all seven labels. Add an overview assertion for `paused`.

```tsx
expect(screen.getAllByRole("listitem").map((row) => row.textContent)).toEqual([
  expect.stringContaining("batch-new"),
  expect.stringContaining("batch-middle"),
  expect.stringContaining("batch-old"),
]);
expect(screen.getByText("batch-paused").closest("li")).toHaveTextContent("已暂停");
```

- [ ] **Step 2: Verify RED**

Run: `npm --prefix evaluation/web test -- --run src/components/ReportIndex.test.tsx src/components/BatchOverview.test.tsx`

Expected: API order is rendered and paused is shown as `进行中`.

- [ ] **Step 3: Implement the shared exhaustive helper**

```ts
export const batchStatusLabels: Record<BatchStatus, string> = {
  queued: "排队中", running: "进行中", pausing: "暂停中", paused: "已暂停",
  cancelling: "取消中", completed: "已完成", cancelled: "已取消",
};

export function newestBatches(batches: readonly BatchRecord[]): BatchRecord[] {
  return [...batches].sort((left, right) =>
    Date.parse(right.created_at) - Date.parse(left.created_at) || right.batch_id.localeCompare(left.batch_id));
}
```

Sort after load without mutating the API array, apply the newest marker after sorting, and remove both fallback status
ternaries.

- [ ] **Step 4: Verify GREEN and commit**

Run the focused tests and `npm --prefix evaluation/web run build`.

```bash
git add evaluation/web/src/batchStatus.ts evaluation/web/src/components/ReportIndex.tsx evaluation/web/src/components/ReportIndex.test.tsx evaluation/web/src/components/BatchOverview.tsx evaluation/web/src/components/BatchOverview.test.tsx
git commit -m "fix(eval): order and label evaluation batches"
```

### Task 2: Exact queue evidence and durable finalization state

**Files:**
- Modify: `evaluation/src/powercontext_eval/tokensflow.py`
- Modify: `evaluation/src/powercontext_eval/web/store.py`
- Modify: `evaluation/tests/unit/test_tokensflow.py`
- Modify: `evaluation/tests/web/test_store.py`

- [ ] **Step 1: Write the RED matrix**

Only a normalized complete line equal to `[PASS] queue: caught up (0 pending files)` passes. `[WARN]`, nonzero pending,
missing prefix, suffix text and `recent uploads` do not. Store tests cover unique `(attempt_id, arm)` registration,
immutable 600-second deadline, oldest-first reads, both state paths and restart reads of intermediate states.

```python
@pytest.mark.parametrize(("raw", "expected"), [
    (b"[PASS] queue: caught up (0 pending files)\n", True),
    (b"[WARN] queue: 2 pending files\n", False),
    (b"[PASS] recent uploads: caught up\n", False),
    (b"[PASS] queue: caught up (0 pending files) extra\n", False),
])
def test_queue_requires_exact_pass_line(raw: bytes, expected: bool) -> None:
    assert tokensflow_queue_caught_up(raw) is expected
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/unit/test_tokensflow.py evaluation/tests/web/test_store.py -q`

Expected: near matches pass incorrectly and finalization store methods are absent.

- [ ] **Step 3: Implement minimal persistence**

Add `FinalizationState` (`pending`, `caught_up`, `timing_out`, `succeeded`, `timed_out`), reasons `deadline` and
`capacity_reclaimed`, create/record models, the `tokensflow_finalizations` table, safe indexes and these methods:

```python
register_tokensflow_finalization(create, now)
list_open_tokensflow_finalizations()
mark_tokensflow_caught_up(job_id, doctor_rc, now)
mark_tokensflow_timing_out(job_id, reason, now)
finish_tokensflow_finalization(job_id, now)
tokensflow_finalizations_for_attempt(attempt_id)
```

Validate safe IDs and run-root-relative paths. Persist no raw output, environment, identity or credential data. Change
the parser to line equality after CRLF normalization.

- [ ] **Step 4: Verify GREEN and commit**

Run the Step 2 command.

```bash
git add evaluation/src/powercontext_eval/tokensflow.py evaluation/src/powercontext_eval/web/store.py evaluation/tests/unit/test_tokensflow.py evaluation/tests/web/test_store.py
git commit -m "feat(eval): persist TokensFlow finalization jobs"
```

### Task 3: Hand off artifact-complete arm containers

**Files:**
- Modify: `evaluation/src/powercontext_eval/powercontext_sut.py`
- Modify: `evaluation/src/powercontext_eval/runner.py`
- Modify: `evaluation/src/powercontext_eval/web/worker.py`
- Modify: `evaluation/tests/contract/test_codex_contract.py`
- Modify: `evaluation/tests/unit/test_runner_phases.py`
- Modify: `evaluation/tests/web/test_worker.py`

- [ ] **Step 1: Write handoff RED tests**

Assert all patch/context/treatment/log artifacts exist before registration; the private task network is disconnected;
egress/container/wrapper/private home remain registered; official evaluation/report continue; task success releases the
lease while the job remains pending and the next task can be claimed. A registration error must clean locally and fail
closed without an untracked container. Standalone CLI runs retain synchronous cleanup.

- [ ] **Step 2: Verify RED**

Run: `uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/contract/test_codex_contract.py evaluation/tests/unit/test_runner_phases.py evaluation/tests/web/test_worker.py -q`

Expected: the SUT drains/removes synchronously and never registers ownership.

- [ ] **Step 3: Implement ownership transfer**

Add an optional `FinalizationRegistrar` to `RunConfig`/`SutConfig`. Worker configs bind it to the store; direct CLI
configs leave it unset. After Codex, retain required traces/evidence/logs, validate treatment, disconnect the private
network, then durably register the safe descriptor. Set `handed_off=True` only after commit. Skip egress/container/
wrapper/private-home cleanup only for successful handoff; every other path keeps current cleanup. Store initial
TokensFlow provenance separately from later terminal finalization facts.

- [ ] **Step 4: Verify GREEN and commit**

Run the Step 2 command.

```bash
git add evaluation/src/powercontext_eval/powercontext_sut.py evaluation/src/powercontext_eval/runner.py evaluation/src/powercontext_eval/web/worker.py evaluation/tests/contract/test_codex_contract.py evaluation/tests/unit/test_runner_phases.py evaluation/tests/web/test_worker.py
git commit -m "feat(eval): hand off TokensFlow container cleanup"
```

### Task 4: Independent bounded finalizer supervisor

**Files:**
- Create: `evaluation/src/powercontext_eval/web/finalization.py`
- Create: `evaluation/tests/web/test_finalization.py`
- Modify: `evaluation/src/powercontext_eval/web/config.py`
- Modify: `evaluation/src/powercontext_eval/web/worker.py`
- Modify: `evaluation/deploy/powercontext-eval.env.example`
- Modify: `evaluation/README.md`
- Modify: `evaluation/tests/web/test_config.py`
- Modify: `evaluation/tests/web/test_worker.py`
- Modify: `evaluation/tests/web/test_deployment.py`

- [ ] **Step 1: Write supervisor RED tests**

Cover exact PASS then graceful stop/cleanup; WARN remains pending; 599 seconds pending; 600 seconds persists
`timing_out` before force cleanup; capacity picks oldest; partial cleanup resumes; restart resumes all intermediate
states; stop returns promptly; no transition changes task, batch, lease or available slot. Log/error assertions allow
only job ID, arm, state, return code and reason.

- [ ] **Step 2: Verify RED**

Run: `uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_finalization.py evaluation/tests/web/test_config.py evaluation/tests/web/test_worker.py evaluation/tests/web/test_deployment.py -q`

Expected: supervisor and settings are absent.

- [ ] **Step 3: Implement one finite poll pass**

`TokensFlowFinalizer.run_once()` processes open jobs without sleeping to deadline. It runs bare `tokensflow doctor` in
the registered container so launch-time `HOME`, binary mount and safe dynamic `TOKENSFLOW_*` remain authoritative.
Persist `caught_up`/`timing_out` before cleanup. Reuse safe daemon stop, exact-container removal, egress detach and
no-follow wrapper cleanup. Timeout/capacity force removal. Start one finalizer thread beside evaluation slots; Worker
stop signals it promptly and leaves rows for restart.

Add validated deployment settings:

```text
POWERCONTEXT_EVAL_TOKENSFLOW_FINALIZER_TIMEOUT_SECONDS=600
POWERCONTEXT_EVAL_TOKENSFLOW_FINALIZER_POLL_SECONDS=5
POWERCONTEXT_EVAL_TOKENSFLOW_FINALIZER_MAX_CONTAINERS=20
```

- [ ] **Step 4: Verify GREEN and commit**

Run the Step 2 command.

```bash
git add evaluation/src/powercontext_eval/web/finalization.py evaluation/src/powercontext_eval/web/config.py evaluation/src/powercontext_eval/web/worker.py evaluation/deploy/powercontext-eval.env.example evaluation/README.md evaluation/tests/web/test_finalization.py evaluation/tests/web/test_config.py evaluation/tests/web/test_worker.py evaluation/tests/web/test_deployment.py
git commit -m "feat(eval): supervise TokensFlow finalization"
```

### Task 5: Sanitized task-detail telemetry

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/batches.py`
- Modify: `evaluation/src/powercontext_eval/web/reporting.py`
- Modify: `evaluation/src/powercontext_eval/web/api.py`
- Modify: `evaluation/tests/web/test_reporting.py`
- Modify: `evaluation/tests/web/test_api.py`
- Modify: `evaluation/web/src/types.ts`
- Modify: `evaluation/web/src/api.ts`
- Modify: `evaluation/web/src/api.test.ts`
- Modify: `evaluation/web/src/components/TaskRunDetail.tsx`
- Modify: `evaluation/web/src/components/TaskRunDetail.test.tsx`

- [ ] **Step 1: Write RED contract/UI tests**

For the selected attempt, require OFF/ON summaries containing only state, timestamps, `queue_caught_up`, `doctor_rc`
and `timed_out_reason`. Recursively reject secret-shaped keys. Render `TokensFlow 收尾中`, `TokensFlow 收尾完成`, or
`TokensFlow 收尾超时` without overriding evaluation success.

- [ ] **Step 2: Verify RED**

Run backend reporting/API tests, then `npm --prefix evaluation/web test -- --run src/api.test.ts src/components/TaskRunDetail.test.tsx`.

Expected: strict schemas and UI do not contain finalization summaries.

- [ ] **Step 3: Implement the allowlisted summary**

Add `TokensFlowFinalizationSummary` and two-arm `tokensflow_finalization` to detail response. Resolve by selected
`attempt_id`, never raw artifacts. Map backend intermediate states to public `pending`; expose `deadline` or
`capacity_reclaimed` only after `timed_out`. Extend Zod/types and add a small independent telemetry section.

- [ ] **Step 4: Verify GREEN and commit**

Run the Step 2 commands.

```bash
git add evaluation/src/powercontext_eval/web/batches.py evaluation/src/powercontext_eval/web/reporting.py evaluation/src/powercontext_eval/web/api.py evaluation/tests/web/test_reporting.py evaluation/tests/web/test_api.py evaluation/web/src/types.ts evaluation/web/src/api.ts evaluation/web/src/api.test.ts evaluation/web/src/components/TaskRunDetail.tsx evaluation/web/src/components/TaskRunDetail.test.tsx
git commit -m "feat(eval): report TokensFlow finalization telemetry"
```

### Task 6: Full regression and controlled m0 activation

**Files:** Verify all Task 1–5 files; this task adds no production behavior.

- [ ] **Step 1: Run local full gates**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml -q
uv run --project evaluation ruff check evaluation
uv run --project evaluation ruff format --check evaluation
uv run --project evaluation ty check --project evaluation
npm --prefix evaluation/web test
npm --prefix evaluation/web run build
git diff --check
```

Expected: every command exits zero and exact counts are recorded.

- [ ] **Step 2: Prove the deployment boundary**

On m0 require both target batches paused and `running_tasks=0`, `active_task_pairs=0`, leases `=0`, Codex usage below
80%, healthy Web/Worker/new-api/MySQL/Redis, healthy SQLite quick-check and safe Docker disk. Abort activation if any
zero condition is false.

- [ ] **Step 3: Validate Linux source and activate evaluation services only**

Run the same gates on m0, back up SQLite, set the three finalizer settings including capacity 20, and deploy. Restart
only evaluation Web/Worker after the zero boundary remains true. Do not restart or reconfigure Docker, new-api, MySQL
or Redis.

- [ ] **Step 4: Accept one controlled task while the batch returns to pause**

Claim exactly one Luna task, immediately request pause, and verify: OFF/ON durable handoff; report success and lease
release independent of finalization; exact queue PASS and graceful cleanup; a disposable short-deadline timeout that
does not change task/batch status; restart recovery; zero residual resources; newest-first UI and Sol `已暂停`; no raw
doctor, identity, config or credential in DB/API/log/report evidence.

- [ ] **Step 5: Resume only Luna at approved capacity**

With usage below 80% and services healthy, set task parallelism 10 at a fresh zero boundary, restart only Worker,
confirm capacity 10 while paused, then explicitly resume Luna. Require at most ten fresh Luna leases, zero Sol leases,
finalizer-owned containers at or below 20, and no infrastructure pause.
