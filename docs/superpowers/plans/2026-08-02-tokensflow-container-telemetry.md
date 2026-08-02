# TokensFlow Host and Evaluation-Container Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start TokensFlow collection on m0 and in every isolated OFF/ON container, dynamically snapshot the selected configuration, require identical host/container identity, and guarantee a complete bounded telemetry drain before task success.

**Architecture:** Add TokensFlow paths to the existing immutable evaluation configuration and propagate them through `RunConfig` into `SutConfig`. A focused `tokensflow.py` module owns safe configuration snapshots, identity hashing, and non-secret lifecycle evidence; `DockerSut` owns container command orchestration. The host runs the TokensFlow-managed user service, while each arm runs one daemon in its existing task container and drains it within 60 seconds before cleanup.

**Tech Stack:** Python 3.11+, Pydantic, pytest, existing `ProcessRunner`/Docker contract tests, systemd user services, TokensFlow 1.0.16 CLI, uv, Ruff, ty.

---

## File Structure

- Create `evaluation/src/powercontext_eval/tokensflow.py`: safe source validation/snapshotting, identity normalization and hashing, lifecycle evidence, drain deadline.
- Modify `evaluation/src/powercontext_eval/web/config.py`: configurable TokensFlow binary and user-home paths.
- Modify `evaluation/src/powercontext_eval/runner.py`: propagate TokensFlow configuration and add its credential variants to artifact redaction.
- Modify `evaluation/src/powercontext_eval/web/worker.py`: map Web configuration into batch and legacy run configurations.
- Modify `evaluation/src/powercontext_eval/cli.py`: propagate configuration through direct/smoke entry points.
- Modify `evaluation/src/powercontext_eval/powercontext_sut.py`: mount TokensFlow, gate identity, start daemon, drain, retain non-secret provenance, preserve spool on failure.
- Modify `evaluation/deploy/powercontext-eval.env.example`: document dynamic source paths.
- Modify `evaluation/README.md`: operator setup, configuration switching, host daemon, controlled rollout, recovery.
- Create `evaluation/tests/unit/test_tokensflow.py`: focused safe-snapshot and evidence behavior.
- Modify `evaluation/tests/web/test_config.py`, `evaluation/tests/web/test_worker.py`, `evaluation/tests/web/test_deployment.py`: propagation and deployment contracts.
- Modify `evaluation/tests/unit/test_runner_phases.py`: runner redaction and arm path propagation.
- Modify `evaluation/tests/contract/test_codex_contract.py`: observable container identity/daemon/drain/isolation behavior.
- Modify `evaluation/tests/unit/test_cli.py`: direct command propagation.

### Task 1: Configurable TokensFlow Sources

**Files:**
- Modify: `evaluation/tests/web/test_config.py`
- Modify: `evaluation/tests/web/test_worker.py`
- Modify: `evaluation/tests/unit/test_cli.py`
- Modify: `evaluation/src/powercontext_eval/web/config.py`
- Modify: `evaluation/src/powercontext_eval/web/worker.py`
- Modify: `evaluation/src/powercontext_eval/runner.py`
- Modify: `evaluation/src/powercontext_eval/cli.py`

- [ ] **Step 1: Write failing configuration and propagation tests**

Add behavior assertions equivalent to:

```python
def test_web_config_reads_dynamic_tokensflow_sources(tmp_path: Path) -> None:
    config = WebConfig.from_environment(
        {
            "POWERCONTEXT_EVAL_ROOT": str(tmp_path),
            "POWERCONTEXT_EVAL_TOKENSFLOW_BINARY": "/opt/tools/tokensflow",
            "POWERCONTEXT_EVAL_TOKENSFLOW_USER_HOME": "/srv/identities/current",
        }
    )

    assert config.tokensflow_binary == Path("/opt/tools/tokensflow")
    assert config.tokensflow_user_home == Path("/srv/identities/current")
```

Extend Worker and CLI mapping tests so both values reach `RunConfig` for batch and legacy tasks. Add both names to the
relative-path rejection matrix and assert they are excluded from public serialization/repr when source paths reveal an
account profile.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_config.py \
  evaluation/tests/web/test_worker.py \
  evaluation/tests/unit/test_cli.py -q
```

Expected: failures because the fields and constructor arguments do not exist.

- [ ] **Step 3: Implement minimal immutable configuration propagation**

Add excluded absolute `Path` fields to `WebConfig`, defaults under the configured root for tests, and named environment
inputs:

```python
tokensflow_binary: Path
tokensflow_user_home: Path = Field(exclude=True, repr=False)
```

Add matching fields to `RunConfig` and `MinimalRunConfig`; pass them through `TaskPairWorker`, direct CLI construction,
and runner-to-SUT mapping. Do not read global `HOME`, `PATH`, or unprefixed environment variables.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit the vertical slice**

```bash
git add evaluation/src/powercontext_eval/{cli.py,runner.py} \
  evaluation/src/powercontext_eval/web/{config.py,worker.py} \
  evaluation/tests/web/{test_config.py,test_worker.py} \
  evaluation/tests/unit/test_cli.py
git commit -m "feat(eval): configure TokensFlow sources"
```

### Task 2: Safe Per-Arm TokensFlow Snapshot

**Files:**
- Create: `evaluation/tests/unit/test_tokensflow.py`
- Create: `evaluation/src/powercontext_eval/tokensflow.py`
- Modify: `evaluation/tests/unit/test_runner_phases.py`
- Modify: `evaluation/src/powercontext_eval/runner.py`

- [ ] **Step 1: Write the complete safe-snapshot test matrix**

Create tests for: regular `credentials.json`, optional `config.toml`, nested regular configuration, fresh destination,
mode 0700/0600, content replacement seen by the next snapshot, and rejection of missing credentials, source-root
symlinks, file symlinks, FIFO/device/socket entries, path escape, and pre-existing destination.

The main observable success test should resemble:

```python
def test_snapshot_tokensflow_home_is_private_and_content_current(tmp_path: Path) -> None:
    source_home = tmp_path / "profile-a"
    config = source_home / ".tokensflow"
    config.mkdir(parents=True, mode=0o700)
    (config / "credentials.json").write_text('{"access":"first"}')
    destination = tmp_path / "arm/runtime/tokensflow-home"

    snapshot = snapshot_tokensflow_home(source_home, destination)

    assert snapshot.user_home == destination
    assert (destination / ".tokensflow/credentials.json").read_text() == '{"access":"first"}'
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / ".tokensflow/credentials.json").stat().st_mode) == 0o600
```

Add a runner test proving TokensFlow credential scalar variants join Codex variants in `ArtifactStore.forbidden_values`
without serializing either source path.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/unit/test_tokensflow.py \
  evaluation/tests/unit/test_runner_phases.py -q
```

Expected: import failure for the not-yet-created TokensFlow module or missing behavior assertions.

- [ ] **Step 3: Implement no-follow recursive snapshot and secret extraction**

Implement focused public types/functions:

```python
@dataclass(frozen=True)
class TokensFlowSnapshot:
    user_home: Path
    credentials: Path


def snapshot_tokensflow_home(source_user_home: Path, destination_user_home: Path) -> TokensFlowSnapshot: ...


def tokensflow_secret_variants(credentials_json: Path) -> tuple[str, ...]: ...
```

Use descriptor-relative traversal with `follow_symlinks=False`, allow directories and regular files only, create a
fresh 0700 tree, copy files through no-follow descriptors at mode 0600, require `.tokensflow/credentials.json`, and
create `.local/share/tokensflow` privately for daemon state. Reuse or extract the existing nested JSON scalar secret
variant logic rather than duplicating encoders.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit the vertical slice**

```bash
git add evaluation/src/powercontext_eval/{tokensflow.py,runner.py} \
  evaluation/tests/unit/{test_tokensflow.py,test_runner_phases.py}
git commit -m "feat(eval): snapshot TokensFlow identity privately"
```

### Task 3: Identity Gate and Container Daemon

**Files:**
- Modify: `evaluation/tests/contract/test_codex_contract.py`
- Modify: `evaluation/src/powercontext_eval/tokensflow.py`
- Modify: `evaluation/src/powercontext_eval/powercontext_sut.py`

- [ ] **Step 1: Write failing observable container-contract tests**

Extend `TranscriptDocker` with safe fake outputs for host/container version and `whoami`. Add tests proving:

- the configured resolved TokensFlow directory is mounted read-only at `/tools/tokensflow-dir`;
- container `HOME` is `/runtime/tokensflow-home` while `CODEX_HOME` remains `/runtime/codex-home`;
- host and container `whoami` execute before Codex;
- only terminal line ending/newline differences are normalized;
- differing identity bytes raise a sanitized infrastructure error with neither identity text present;
- exactly one detached daemon starts per active arm after identity match;
- OFF, ON, and concurrent run IDs use distinct writable homes and daemon PID files;
- a non-executable or unexpected binary fails before inference.

Example identity assertion:

```python
with pytest.raises(TokensFlowInfrastructureError, match="identity did not match") as captured:
    sut.run_arm(config, Arm.ON, paths, b"prompt", store)
assert "host-person" not in str(captured.value)
assert "container-person" not in str(captured.value)
assert not any(command[-2:] == ("codex", "exec") for command in docker.commands)
```

- [ ] **Step 2: Run the contract tests and verify RED**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/contract/test_codex_contract.py -q
```

Expected: missing TokensFlow mount, identity commands, and daemon start.

- [ ] **Step 3: Implement identity evidence and daemon startup**

Add non-secret evidence:

```python
@dataclass(frozen=True)
class TokensFlowEvidence:
    host_version: str
    container_version: str
    host_identity_sha256: str
    container_identity_sha256: str
    identity_bytes: int
    identity_match: bool
    daemon_started: bool
```

Generalize `_tool_directory_mount` to accept the expected configured tool name rather than keeping the current
`{"codex", "uv"}` set. Snapshot the arm home before start, mount the selected binary, execute both `whoami` commands
with bounded timeouts, compare normalized bytes, and start the daemon through a detached `docker exec` wrapper that
writes a private PID file and private log under `/runtime/tokensflow-home`.

Do not include raw identity, source paths, daemon output, configuration, or credentials in exceptions or artifacts.

- [ ] **Step 4: Run the contract tests and verify GREEN**

Run the Step 2 command. Expected: all contract tests pass.

- [ ] **Step 5: Commit the vertical slice**

```bash
git add evaluation/src/powercontext_eval/{tokensflow.py,powercontext_sut.py} \
  evaluation/tests/contract/test_codex_contract.py
git commit -m "feat(eval): run TokensFlow inside task containers"
```

### Task 4: Bounded Zero-Loss Drain and Recovery

**Files:**
- Modify: `evaluation/tests/unit/test_tokensflow.py`
- Modify: `evaluation/tests/contract/test_codex_contract.py`
- Modify: `evaluation/src/powercontext_eval/tokensflow.py`
- Modify: `evaluation/src/powercontext_eval/powercontext_sut.py`
- Modify: `evaluation/src/powercontext_eval/web/worker.py`

- [ ] **Step 1: Write failing drain behavior tests**

Declare the matrix before implementation:

| Case | Expected behavior |
| --- | --- |
| normal stop/upload/status | arm continues and cleanup removes container |
| duplicate replay | success remains valid |
| daemon TERM timeout | infrastructure failure, no successful cleanup of only spool |
| upload non-zero | infrastructure failure and batch pause |
| status pending/blocked | infrastructure failure and preserved private data |
| combined operations exceed 60 seconds | no later command receives a fresh 60 seconds; failure at shared deadline |
| secret in command output | sanitized failure; raw content never retained |

Use an injected monotonic clock and transcript runner. Assert order only where it expresses the external zero-loss
contract: Codex completes, daemon terminates, `upload --all` succeeds, queue reports caught up, provenance is written,
then cleanup occurs.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/unit/test_tokensflow.py \
  evaluation/tests/contract/test_codex_contract.py \
  evaluation/tests/web/test_worker.py -q
```

Expected: missing drain deadline and failure-preservation behavior.

- [ ] **Step 3: Implement one 60-second drain budget**

Implement a deadline object:

```python
@dataclass
class DrainDeadline:
    timeout_seconds: float = 60.0
    clock: Callable[[], float] = time.monotonic

    def remaining(self) -> float:
        remaining = self._deadline - self.clock()
        if remaining <= 0:
            raise TokensFlowInfrastructureError("TokensFlow drain timed out")
        return remaining
```

After Codex returns, use remaining budget for TERM/join, `tokensflow upload --all`, and non-secret queue inspection.
Accept duplicate replay. Require a successful command and caught-up queue. On failure, write only safe evidence to the
private attempt runtime, raise the infrastructure error, let the Worker atomically persist failure/pause, and skip
destructive deletion of the only TokensFlow/Codex spool. Add an explicit recovery-path marker so a retry cannot mistake
the preserved runtime for a clean attempt.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit the vertical slice**

```bash
git add evaluation/src/powercontext_eval/{tokensflow.py,powercontext_sut.py} \
  evaluation/src/powercontext_eval/web/worker.py \
  evaluation/tests/unit/test_tokensflow.py \
  evaluation/tests/contract/test_codex_contract.py \
  evaluation/tests/web/test_worker.py
git commit -m "feat(eval): drain TokensFlow before task success"
```

### Task 5: Deployment and Operator Contracts

**Files:**
- Modify: `evaluation/tests/web/test_deployment.py`
- Modify: `evaluation/deploy/powercontext-eval.env.example`
- Modify: `evaluation/README.md`
- Modify: `evaluation/src/powercontext_eval/cli.py`
- Modify: `evaluation/tests/unit/test_cli.py`

- [ ] **Step 1: Write failing deployment documentation tests**

Require the example environment to contain exactly the two new named variables, with no credential content. Require
the operator guide to document configuration replacement versus path switching, host-user-service activation,
`tokensflow whoami` hash-only comparison, daemon verification, 60-second drain, spool recovery, controlled single-task
rollout, and rollback without Docker/new-api/MySQL/Redis mutation.

Extend the contract-smoke CLI tests to require TokensFlow paths and return only safe evidence.

- [ ] **Step 2: Run deployment tests and verify RED**

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_deployment.py \
  evaluation/tests/unit/test_cli.py -q
```

Expected: missing environment keys, documentation terms, and smoke parameters.

- [ ] **Step 3: Implement deployment assets and smoke inputs**

Add:

```dotenv
POWERCONTEXT_EVAL_TOKENSFLOW_BINARY=/usr/local/bin/tokensflow
POWERCONTEXT_EVAL_TOKENSFLOW_USER_HOME=/home/rongfeng.frf
```

Document enabling the existing user unit with linger and a user manager, but do not add a second root service or
unmanaged background process. Update contract smoke to exercise identity, daemon readiness, and complete drain without
printing identity or credentials.

- [ ] **Step 4: Run deployment tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit the vertical slice**

```bash
git add evaluation/deploy/powercontext-eval.env.example evaluation/README.md \
  evaluation/src/powercontext_eval/cli.py \
  evaluation/tests/web/test_deployment.py evaluation/tests/unit/test_cli.py
git commit -m "docs(eval): operate TokensFlow telemetry safely"
```

### Task 6: Full Verification and m0 Controlled Rollout

**Files:**
- Verify all files changed above.
- Update the protected m0 environment outside Git; do not expose its values.

- [ ] **Step 1: Run complete local verification**

```bash
uv run --project evaluation pytest \
  -c evaluation/pyproject.toml evaluation/tests -m "not live" -q
uv run --project evaluation ruff check evaluation/src evaluation/tests
uv run --project evaluation ruff format --check evaluation/src evaluation/tests
uv run --directory evaluation ty check src tests
npm --prefix evaluation/web test
npm --prefix evaluation/web run build
```

Expected: every command exits zero with explicit test counts.

- [ ] **Step 2: Pause the current batch at a clean boundary**

Request batch pause, allow active task pairs to finish, and verify `running=0`, `leases=0`, no evaluation container,
usage below 80 percent, healthy Web/Worker/new-api/MySQL/Redis, and safe disk space. Do not cancel attempts.

- [ ] **Step 3: Deliver source and repeat complete Linux verification on m0**

Push normally when the repository remote accepts it; otherwise use the existing Git-bundle source transport. On m0,
check out the exact commit and run the same backend/frontend/lint/format/full-ty/build matrix. Do not deploy a Mac-built
artifact.

- [ ] **Step 4: Enable and verify the host TokensFlow service**

Using the existing TokensFlow-managed user unit:

```bash
sudo loginctl enable-linger rongfeng.frf
sudo systemctl start user@2950.service
sudo -u rongfeng.frf env XDG_RUNTIME_DIR=/run/user/2950 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/2950/bus \
  systemctl --user enable --now tokensflow.service
```

Verify exactly one service-owned host daemon, active user unit, successful status, and successful host `whoami` without
printing identity or credentials. If the user manager is unavailable, stop; do not use `nohup` or a root replacement.

- [ ] **Step 5: Deploy Worker configuration and disposable smoke**

Add the two protected environment settings, restart only `powercontext-eval-worker.service`, confirm the batch remains
paused, then run the real disposable task-image smoke. Compare host/container `whoami` hashes and lengths, observe one
container daemon, generate a Codex JSONL, verify graceful stop plus `upload --all` and caught-up queue within 60 seconds,
and confirm cleanup.

- [ ] **Step 6: Run one controlled logical task**

Explicitly resume, wait for exactly one task claim, immediately request pause, and allow OFF and ON to complete. Verify
distinct homes/scopes, daemon presence during each arm, identity evidence, complete drain, PowerContext ON injection,
official evaluation, report, cleanup, services, usage, and disk.

- [ ] **Step 7: Restore sustained capacity and resume**

Only after the controlled task succeeds and the batch has `running=0`, restore the prior task parallelism, restart only
Worker, confirm capacity while still paused, and explicitly resume. Update the monitor baseline and add host/container
TokensFlow daemon/drain checks.

- [ ] **Step 8: Final source commit if verification required adjustments**

If verification changed tracked source, rerun all affected and full checks before committing:

```bash
git add evaluation docs/superpowers/specs/2026-08-02-tokensflow-container-telemetry-design.md \
  docs/superpowers/plans/2026-08-02-tokensflow-container-telemetry.md
git commit -m "feat(eval): collect container Codex telemetry"
```
