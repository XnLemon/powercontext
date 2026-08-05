# Root Evaluation Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the SWE-bench task images' native root execution semantics and remove evaluator-created permission failures.

**Architecture:** The root Worker launches disposable task containers without user, HOME, read-only-root, capability,
or no-new-privileges overrides. A per-arm root home is mounted at `/root`, so Codex and TokensFlow use their default
paths while retaining isolated dynamic configuration and finalization state. Successful tasks clean up normally;
infrastructure failures retain their container and related resources until an operator explicitly cleans them up.

**Tech Stack:** Python 3.11, pytest, Docker CLI, systemd, uv

---

### Task 1: Executable container contract

**Files:**
- Modify: `evaluation/tests/contract/test_codex_contract.py`
- Modify: `evaluation/src/powercontext_eval/powercontext_sut.py`

- [ ] Replace assertions requiring `--user 2950:100`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, `HOME`,
  and `CODEX_HOME` with assertions that executable task/setup/fallback commands omit them.
- [ ] Run `uv run pytest evaluation/tests/contract/test_codex_contract.py -q` and confirm the new assertions fail
  against the current hardened command transcript.
- [ ] Remove the command arguments and the `_assign_arm_ownership` stage, mount the arm's private home at `/root`,
  and leave resource limits, scoped networks/mounts, and Docker-socket exclusion unchanged.
- [ ] Re-run the contract tests and confirm they pass.

### Task 2: Root-owned runtime layout

**Files:**
- Modify: `evaluation/src/powercontext_eval/runner.py`
- Modify: `evaluation/src/powercontext_eval/powercontext_sut.py`
- Modify: `evaluation/tests/contract/test_codex_contract.py`

- [ ] Build each arm with one private root home, `codex_home=root_home / ".codex"`, and TokensFlow snapshot destination
  `root_home`; update lifecycle commands to use `/root/.codex`, `/root/.tokensflow`, and `/root/.local/share/tokensflow`.
- [ ] Verify Codex plugin setup, runtime inference, TokensFlow identity, daemon, drain, and cleanup tests use default
  paths without HOME overrides.

### Task 3: Root Worker deployment contract

**Files:**
- Modify: `evaluation/deploy/powercontext-eval-worker.service`
- Modify: `evaluation/tests/web/test_deployment.py`
- Modify: `evaluation/README.md`

- [ ] Change only the Worker unit to root by removing `User=` and `Group=`; keep the Web unit unchanged.
- [ ] Update deployment assertions and operator instructions, then run `uv run pytest evaluation/tests/web/test_deployment.py -q`.

### Task 4: Verification and controlled deployment

- [ ] Run `uv run pytest evaluation/tests -q`, Ruff, format, and `ty` locally and on m0 Linux.
- [ ] With both batches paused and no active leases, install the Worker unit/code and restart only the Worker.
- [ ] Retry one failed/queued Luna task and verify root identity, `/root/.codex`, `/root/.tokensflow`, Codex, TokensFlow
  finalization, official grading, reporting, success cleanup, and infrastructure-failure retention.
- [ ] If the proof task succeeds and services are healthy, restore normal task parallelism and resume the paused
  Luna batches.
