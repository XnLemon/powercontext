# Evaluation Service Shutdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Worker and Web stop cleanly under systemd while promptly cancelling and durably releasing active TokensFlow finalization work.

**Architecture:** Guard the Python signal boundary against reentry, propagate the existing Worker stop Event through finalizer runtime commands, and terminate only child process groups created by the current ProcessRunner call. Preserve durable jobs by releasing cancelled leases to pending. Harden both systemd units against uv double delivery and accept only Web's graceful 143 status.

**Tech Stack:** Python 3.11+, threading, subprocess, pytest, systemd unit files.

---

### Task 1: Idempotent Worker signal handling

**Files:**
- Modify: `evaluation/tests/web/test_cli.py`
- Modify: `evaluation/src/powercontext_eval/cli.py`

- [x] **Step 1: Write the failing subprocess regression**

Use `_worker_signal_handlers` in a Python subprocess. Its `stop()` acquires a normal Lock, signals a helper thread to send a second SIGTERM, briefly holds the lock, then records completion. Assert the subprocess exits within one second and writes exactly one stop marker.

- [x] **Step 2: Run the focused test and verify RED**

Run: `uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_cli.py -q`

Expected: the new subprocess test times out because the second handler re-enters `stop()`.

- [x] **Step 3: Add the minimal once guard**

```python
stop_requested = False

def handler(signum: int, frame: FrameType | None) -> None:
    nonlocal stop_requested
    if stop_requested:
        return
    stop_requested = True
    _request_worker_stop(worker, signum, frame)
```

- [x] **Step 4: Re-run the focused test and verify GREEN**

Expected: subprocess exits within the budget and the existing single-signal test still proves one signal calls `stop()`.

### Task 2: Cooperative finalizer process cancellation

**Files:**
- Modify: `evaluation/tests/web/test_finalization.py`
- Modify: `evaluation/tests/unit/test_process.py`
- Modify: `evaluation/src/powercontext_eval/errors.py`
- Modify: `evaluation/src/powercontext_eval/process.py`
- Modify: `evaluation/src/powercontext_eval/web/finalization.py`

- [x] **Step 1: Write failing upload and doctor shutdown tests**

For each phase, run `TokensFlowFinalizer.run_forever` with a durable registered job and a blocking runtime that observes the passed stop Event. Set stop after the selected phase enters. Assert bounded thread exit, state `pending`, no lease owner/expiry, and immediate claim by a replacement worker.

- [x] **Step 2: Write the failing ProcessRunner cancellation test**

Launch a fixture process with `start_new_session=True`, cancel after it reports readiness, and assert the runner terminates its own process group while an unrelated sentinel process remains alive.

- [x] **Step 3: Run the focused tests and verify RED**

Run: `uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_finalization.py evaluation/tests/unit/test_process.py -q`

Expected: the stop Event cannot be supplied to runtime commands and blocking commands remain alive.

- [x] **Step 4: Implement cancellation and durable release**

Add an optional cancellation Event to `ProcessRunner.run`. Poll `communicate` in bounded intervals; when cancellation is set, terminate the exact child PID/process group created by that call and raise a sanitized `CommandCancelled`. Pass the Event through finalizer `run_once`, job processing, and Docker runtime methods. Before claiming new work after stop, return without claiming. When cancellation interrupts an owned job, call `release_tokensflow_finalization(..., error_category="shutdown")` so it is immediately pending.

- [x] **Step 5: Re-run focused tests and verify GREEN**

Expected: both blocked phases stop within budget, leases are immediately reclaimable, and the unrelated sentinel survives.

### Task 3: systemd shutdown contract

**Files:**
- Modify: `evaluation/tests/web/test_deployment.py`
- Modify: `evaluation/deploy/powercontext-eval-worker.service`
- Modify: `evaluation/deploy/powercontext-eval-web.service`

- [x] **Step 1: Write the failing unit contract**

Require `KillMode=mixed` in both units. Require `SuccessExitStatus=143` in Web only and explicitly reject `137` from all success-status declarations.

- [x] **Step 2: Run deployment tests and verify RED**

Run: `uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_deployment.py -q`

- [x] **Step 3: Add the minimal unit directives**

```ini
KillMode=mixed
```

Add to Web only:

```ini
SuccessExitStatus=143
```

- [x] **Step 4: Re-run deployment tests and verify GREEN**

### Task 4: Verification and commit

**Files:**
- Verify all modified files and generated frontend assets.

- [x] **Step 1: Run shutdown-focused tests**

Run the CLI, Worker, finalization, process, and deployment test files together.

- [x] **Step 2: Run backend checks**

Run all non-live evaluation tests, Ruff, formatting check, and ty.

- [x] **Step 3: Run frontend/static/build checks**

Use the repository's pinned frontend test and build commands, then verify tracked static output matches the build contract.

- [x] **Step 4: Review and amend**

Run `git diff --check`, inspect the complete diff, confirm only the pre-existing `node_modules/` remains unrelated, stage the intended files, and amend commit `a54ad14` with the existing subject.
