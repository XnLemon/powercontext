# Source559 Official Proxy Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the infrastructure Docker proxy from changing source559's official test semantics while preserving proxy behavior for every other task and recording the exception in the report.

**Architecture:** Extend the exact-instance Gold selection with a strict official-evaluation transport policy. Select the policy before creating the one evaluator shared by Gold/OFF/ON, and bind the policy into the existing Gold audit model and report.

**Tech Stack:** Python 3.11+, Pydantic v2, pytest, Ruff, ty, pinned SWE-bench Pro evaluator.

---

### Task 1: Lock the runner and audit behavior with failing tests

**Files:**
- Modify: `evaluation/tests/unit/test_runner_phases.py`
- Modify: `evaluation/tests/unit/test_gold_overrides.py`

- [ ] **Step 1: Add a runner regression for source559**

Extend `test_source559_gold_override_preserves_original_row_and_off_on_patches` to inspect
`evaluator_initializations`, require exactly one initialization, require no `proxy` keyword, and retain the existing
three-call Gold/OFF/ON assertions.

- [ ] **Step 2: Add audit transport expectations**

Require source559 selection and final `GoldValidationAudit` to contain
`official_evaluation_transport="proxy_bypassed_for_test_isolation"`. Add a tampered source559 audit case using
`docker_proxy` and require strict validation to fail. Require ordinary/legacy audit input without the field to parse as
`docker_proxy`.

- [ ] **Step 3: Verify RED**

Run:

```bash
cd evaluation
uv run pytest tests/unit/test_runner_phases.py::test_source559_gold_override_preserves_original_row_and_off_on_patches tests/unit/test_gold_overrides.py -q
```

Expected: failures show source559 still constructs a proxy-configured evaluator and the transport field is absent.

### Task 2: Implement the strict transport policy

**Files:**
- Modify: `evaluation/src/powercontext_eval/benchmarks/swebench_pro/gold_overrides.py`
- Modify: `evaluation/src/powercontext_eval/report.py`
- Modify: `evaluation/src/powercontext_eval/runner.py`

- [ ] **Step 1: Add the audited transport field**

Add `official_evaluation_transport` to `GoldValidationSelection.audit` and `GoldValidationAudit`. Default ordinary
selection/audit data to `docker_proxy`; select `proxy_bypassed_for_test_isolation` only after source559's exact dataset
and reference hashes pass. Require that value in source559 provenance validation.

- [ ] **Step 2: Select transport before evaluator construction**

Move `select_gold_validation(instance.instance_id, instance.patch)` before evaluator construction. Construct
`OfficialEvaluator(..., proxy=ProxyRelayConfig(config.proxy_url))` for `docker_proxy`, and omit the proxy for the
source559 bypass. Reuse the resulting evaluator for all three official calls.

- [ ] **Step 3: Render the transport audit**

Add the transport field to the Gold validation section without exposing any proxy URL or environment value.

- [ ] **Step 4: Verify GREEN**

Run the RED command again. Expected: all selected tests pass.

- [ ] **Step 5: Run focused evaluator and reporting tests**

```bash
cd evaluation
uv run pytest tests/contract/test_swebench_contract.py tests/unit/test_runner_phases.py tests/unit/test_gold_overrides.py tests/web/test_api.py -q
```

Expected: all tests pass and ordinary proxy behavior remains covered.

### Task 3: Validate, deploy, and run the controlled retry

**Files:**
- No additional source files.

- [ ] **Step 1: Run the complete local validation matrix**

```bash
cd evaluation
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run ty check src
cd web/frontend
npm test -- --run
```

Expected: backend, lint, format, source type checks, and frontend tests pass; record exact counts.

- [ ] **Step 2: Commit and push only the scoped source, tests, spec, and plan**

Stage explicit paths so the existing modified manual retry script and its test remain untouched. Use a concise
Conventional Commit subject and push `codex/swebench-pro-eval`.

- [ ] **Step 3: Verify and deploy on m0**

Keep the batch paused/running0. Transfer by rsync or verified Git bundle through `m0-root`, fast-forward to the exact
commit, run the same backend/static validation, then restart only the idle evaluation Worker.

- [ ] **Step 4: Run attempt6 under a one-task boundary**

Set parallelism to 1 without printing the environment, retry only source559, resume until the unique attempt6 claim is
observed, then immediately pause. Verify Gold, OFF, ON, official output, transport audit, and report.

- [ ] **Step 5: Restore the main queue**

If attempt6 succeeds and usage/service/storage gates pass, restore parallelism 20, restart only the idle Worker, confirm
the batch remains paused, then explicitly resume. If attempt6 fails, preserve evidence and do not retry or resume.

