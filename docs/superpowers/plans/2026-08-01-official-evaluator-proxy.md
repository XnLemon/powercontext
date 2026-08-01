# Official Evaluator Proxy Propagation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every pinned SWE-bench Pro gold/OFF/ON official evaluator container a task-scoped, container-reachable proxy without changing the pinned harness or global Docker configuration.

**Architecture:** `OfficialEvaluator` will start the existing credential-free `SocatProxyRelay` on Docker's default bridge gateway for each official evaluation call. It will create a private temporary Docker client config whose `proxies.default` points only to that relay, pass the config directory to the pinned harness through `DOCKER_CONFIG`, and clean both resources on success or failure. `runner.py` will pass the already-validated `POWERCONTEXT_EVAL_PROXY_URL` into the evaluator; no retry is added because the root cause is missing proxy propagation.

**Tech Stack:** Python 3.11+, pytest, Docker SDK proxy-config contract, existing `ProcessRunner`, `ProxyRelayConfig`, and `SocatProxyRelay`.

---

### Task 1: Protect the official evaluator proxy boundary with failing tests

**Files:**
- Modify: `evaluation/tests/contract/test_swebench_contract.py`
- Modify: `evaluation/tests/unit/test_runner_phases.py`

- [ ] **Step 1: Write the runner propagation RED test**

Extend the existing fake `OfficialEvaluator` used by `_run_with_fakes` so it records the constructor's proxy configuration, then add a test that runs one fake task with `config.proxy_url == "http://127.0.0.1:7890"` and asserts the same `ProxyRelayConfig` is used for gold, OFF, and ON official evaluation.

- [ ] **Step 2: Write the evaluator lifecycle RED tests**

Add a fake relay with `start(gateway, upstream) -> "http://172.17.0.1:45678"` and `stop()` recording. Add contract tests that assert:

```python
config = json.loads((Path(captured_env["DOCKER_CONFIG"]) / "config.json").read_text())
assert config == {
    "proxies": {
        "default": {
            "httpProxy": "http://172.17.0.1:45678",
            "httpsProxy": "http://172.17.0.1:45678",
            "noProxy": LOOPBACK_NO_PROXY,
        }
    }
}
```

The fake harness must read and record the config during `ProcessRunner.run`, because the directory must no longer exist after `evaluate()` returns. Assert the upstream URL is absent from retained evaluator logs, relay `stop()` runs, and the temporary directory is deleted after both successful evaluation and a raised process error. Preserve a no-proxy compatibility test asserting no `DOCKER_CONFIG` override and no relay activity.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
cd evaluation
uv run pytest tests/contract/test_swebench_contract.py tests/unit/test_runner_phases.py -q
```

Expected: the new tests fail because `OfficialEvaluator` has no proxy/relay inputs and the runner does not pass one. Existing tests must remain green.

### Task 2: Implement the smallest task-scoped proxy propagation

**Files:**
- Modify: `evaluation/src/powercontext_eval/benchmarks/swebench_pro/evaluator.py`
- Modify: `evaluation/src/powercontext_eval/powercontext_sut.py`
- Modify: `evaluation/src/powercontext_eval/runner.py`
- Test: `evaluation/tests/contract/test_swebench_contract.py`
- Test: `evaluation/tests/unit/test_runner_phases.py`

- [ ] **Step 1: Add a reusable safe default-bridge gateway query**

Add a helper in `powercontext_sut.py` that invokes:

```python
process.run(("docker", "network", "inspect", "bridge", "--format={{(index .IPAM.Config 0).Gateway}}"), cwd=cwd, timeout=30)
```

Validate the stripped result with the existing gateway validator before returning it. The helper must not create, remove, or reconfigure Docker networks.

- [ ] **Step 2: Add private Docker proxy-config lifecycle to `OfficialEvaluator`**

Accept an optional `ProxyRelayConfig`, relay factory, and gateway resolver. When proxying is enabled:

1. Resolve the default bridge gateway.
2. Start the relay and receive a credential-free container URL.
3. Create a `TemporaryDirectory` under the evaluation output's parent with mode `0o700`.
4. Create `config.json` with mode `0o600` containing only `proxies.default.httpProxy`, `httpsProxy`, and `noProxy`.
5. Add only `DOCKER_CONFIG=<temporary directory>` to the pinned harness process environment, alongside existing fake-test variables.
6. In `finally`, stop the relay; the temporary directory context deletes the config.

Do not pass the upstream proxy URL to the harness, command line, artifacts, reports, or logs. Do not add retries or change `--block_network`/official CLI arguments.

- [ ] **Step 3: Wire the existing run configuration**

Construct `OfficialEvaluator` in `runner.py` with `ProxyRelayConfig(config.proxy_url)`. Reuse that evaluator for gold, OFF, and ON so every official container receives the same behavior without altering the pinned harness source.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
cd evaluation
uv run pytest tests/contract/test_swebench_contract.py tests/unit/test_runner_phases.py -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check src tests
```

Expected: all focused tests and static checks pass.

- [ ] **Step 5: Commit the TDD fix**

```bash
git add docs/superpowers/plans/2026-08-01-official-evaluator-proxy.md \
  evaluation/src/powercontext_eval/benchmarks/swebench_pro/evaluator.py \
  evaluation/src/powercontext_eval/powercontext_sut.py \
  evaluation/src/powercontext_eval/runner.py \
  evaluation/tests/contract/test_swebench_contract.py \
  evaluation/tests/unit/test_runner_phases.py
git commit -m "fix(eval): proxy official evaluator containers"
```

### Task 3: Verify Linux behavior and recover only the failed task

**Files:**
- No additional source files expected.

- [ ] **Step 1: Run the complete local regression suite**

Run all backend tests, all frontend tests, Ruff, format check, complete `ty check src tests`, frontend build, and `git diff --check`. Record exact pass counts.

- [ ] **Step 2: Deploy from Linux source and run the full m0 regression suite**

Push the commit to the m0 bare `evaluation` ref, update `/data/powercontext-eval/deploy/powercontext` from Linux source, run the same complete checks on m0, and restart only the evaluation Worker. Do not restart or reconfigure Docker, Web, new-api, MySQL, or Redis.

- [ ] **Step 3: Run a real pinned-harness proxy smoke**

Using the deployed code and the source39 task image, verify the Docker SDK version, that a temporary official evaluator container receives all expected proxy variables through its private Docker config, and that `proxy.golang.org` is reachable. The temporary container must be removed and no proxy credentials may be printed or retained.

- [ ] **Step 4: Retry only source39 while paused, then explicitly resume**

Keep the batch paused and task parallelism at 1. Retry only source39, confirm exactly one queued attempt and no other task claim, then explicitly resume only if usage is below 80% and all services are healthy. After source39 succeeds end-to-end, restore task parallelism to 4 at a clean task boundary, restart only the Worker, and explicitly resume sustained execution after health/capacity validation.
