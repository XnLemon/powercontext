# Preserve Batch Pause On Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retrying a failed task must preserve a paused batch's pause state while leaving runnable batches runnable.

**Architecture:** Keep retry attempt creation unchanged. Remove the unconditional batch transition to `run`; retry should only update the logical task and append its retry control event, so the batch's existing control intent remains authoritative.

**Tech Stack:** Python 3.11+, SQLite, pytest, FastAPI task store.

---

### Task 1: Preserve batch control intent during task retry

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/store.py`
- Test: `evaluation/tests/web/test_store.py`

- [x] **Step 1: Write the failing paused-batch behavior test**

Add a store-level test that creates and fails a retryable task, pauses the batch, calls `retry_failed_task`, and asserts that the new attempt is queued while `get_batch(...).status` remains `PAUSED` with the original pause reason.

- [x] **Step 2: Run the focused test and verify RED**

Run:

```bash
uv run --project evaluation pytest evaluation/tests/web/test_store.py::test_retry_in_a_paused_batch_preserves_pause_control -q
```

Expected: FAIL because the current implementation changes `control_intent` to `run` and clears `pause_reason`.

- [x] **Step 3: Implement the minimal fix**

Remove only the unconditional `UPDATE batches SET control_intent = run, pause_reason = NULL ...` statement from `TaskStore.retry_failed_task`. Preserve the existing retry attempt, task reset, and control-event behavior.

- [x] **Step 4: Verify GREEN and existing runnable behavior**

Run:

```bash
uv run --project evaluation pytest evaluation/tests/web/test_store.py -q
```

Expected: all store tests pass, including the existing assertion that retrying a failed task in a runnable batch leaves the batch queued.

- [x] **Step 5: Run the complete evaluation regression suite**

Run:

```bash
uv run --project evaluation pytest evaluation/tests -q
uv run --project evaluation ruff check evaluation/src evaluation/tests
```

Expected: all tests and lint checks pass.

- [ ] **Step 6: Commit and deploy from Linux source**

Commit the test and minimal store change, push `codex/swebench-pro-eval`, pull it on m0, run the same test suite there, and restart only the PowerContext evaluation Web/Worker services. Confirm new-api, MySQL, and Redis remain untouched and healthy.

- [ ] **Step 7: Validate paused retries before resuming**

With the batch still paused, retry only source indices 19–26 using unique idempotency keys. Confirm all eight new attempts are queued, the batch remains paused, and no task is claimed. Resume explicitly only after usage is below 80% and all services are healthy.
