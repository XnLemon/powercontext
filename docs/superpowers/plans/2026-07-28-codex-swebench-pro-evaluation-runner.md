# Codex SWE-bench Pro Evaluation Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify an independent runner that executes one pinned SWE-bench Pro task with Codex under PowerContext OFF and ON, grades both patches with the official evaluator, and emits an auditable comparison report.

**Architecture:** A nested `evaluation/` uv project owns orchestration and artifacts while materializing the PowerContext revision under test into disposable arm environments. Trusted controller code prepares a clean Codex editing container and a separate credential-free official evaluator environment; all moving inputs resolve to immutable provenance before either arm starts.

**Tech Stack:** Python 3.11+, uv, Hatchling, Pydantic 2, Typer, pytest, Git CLI, Docker CLI, Codex CLI, PowerContext Server, official `scaleapi/SWE-bench_Pro-os` evaluator.

---

## Fixed inputs and invariants

- Harness SHA: `ca10a60a5fcae51e6948ffe1485d4153d421e6c5`
- Dataset revision: `7ab5114912baf22bb098818e604c02fe7ad2c11f`
- Initial instance:
  `instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9`
- OFF and ON use identical resolved inputs; the Codex `plugins` feature is the only treatment switch.
- A Gold Patch failure prevents both paid arms.
- ON without prompt-hook Source evidence is `invalid_treatment`.
- OFF with any prompt Source or PowerContext MCP activity is `invalid_treatment`.
- `CODEX_HOME` and PowerContext databases live under `work/`, never under retained `runs/`.
- Existing `m0` Docker containers are not stopped, recreated, or restarted by this plan.

## File map

```text
evaluation/
├── pyproject.toml
├── uv.lock
├── README.md
├── deploy/m0/
│   ├── README.md
│   └── powercontext-eval.env.example
├── src/powercontext_eval/
│   ├── __init__.py
│   ├── artifacts.py
│   ├── cli.py
│   ├── codex.py
│   ├── doctor.py
│   ├── errors.py
│   ├── git_source.py
│   ├── models.py
│   ├── paths.py
│   ├── powercontext_sut.py
│   ├── process.py
│   ├── report.py
│   ├── runner.py
│   └── benchmarks/
│       ├── __init__.py
│       ├── base.py
│       └── swebench_pro/
│           ├── __init__.py
│           ├── adapter.py
│           ├── evaluator.py
│           └── prediction.py
└── tests/
    ├── contract/
    │   ├── fixtures/fake_codex.py
    │   ├── fixtures/fake_evaluator.py
    │   ├── test_codex_contract.py
    │   ├── test_git_contract.py
    │   └── test_swebench_contract.py
    ├── e2e/test_smoke.py
    └── unit/
        ├── test_artifacts.py
        ├── test_models.py
        ├── test_paths.py
        ├── test_process.py
        ├── test_report.py
        └── test_runner.py
```

### Task 1: Scaffold the independent evaluation package and immutable run model

**Files:**
- Create: `evaluation/pyproject.toml`
- Create: `evaluation/src/powercontext_eval/__init__.py`
- Create: `evaluation/src/powercontext_eval/errors.py`
- Create: `evaluation/src/powercontext_eval/models.py`
- Create: `evaluation/src/powercontext_eval/paths.py`
- Create: `evaluation/src/powercontext_eval/cli.py`
- Create: `evaluation/tests/unit/test_models.py`
- Create: `evaluation/tests/unit/test_paths.py`

- [ ] **Step 1: Write failing model and path tests**

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from powercontext_eval.models import Arm, PowerContextRef
from powercontext_eval.paths import EvaluationPaths


def test_powercontext_ref_requires_a_typed_unambiguous_ref() -> None:
    assert PowerContextRef.parse("latest").kind == "latest"
    assert PowerContextRef.parse("branch:main").value == "main"
    assert PowerContextRef.parse("tag:v0.1.0").value == "v0.1.0"
    assert PowerContextRef.parse("commit:" + "a" * 40).value == "a" * 40
    with pytest.raises(ValueError):
        PowerContextRef.parse("main")


def test_retained_and_disposable_paths_are_disjoint(tmp_path: Path) -> None:
    paths = EvaluationPaths(root=tmp_path, run_id="run-01")
    assert paths.arm_work(Arm.ON) == tmp_path / "work/run-01/on"
    assert paths.arm_artifacts(Arm.ON) == tmp_path / "runs/run-01/arms/on"
    assert not paths.arm_work(Arm.ON).is_relative_to(paths.run_artifacts)
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --project evaluation pytest evaluation/tests/unit/test_models.py evaluation/tests/unit/test_paths.py -q
```

Expected: collection fails because `powercontext_eval` and the requested models do not exist.

- [ ] **Step 3: Add the nested project and minimal immutable types**

`evaluation/pyproject.toml` must declare:

```toml
[project]
name = "powercontext-eval"
version = "0.1.0"
requires-python = ">=3.11,<4.0"
dependencies = ["pydantic>=2.10,<3", "typer>=0.16,<1"]

[project.scripts]
powercontext-eval = "powercontext_eval.cli:main"

[dependency-groups]
dev = ["pytest>=9.0.2", "ruff>=0.15.7", "ty>=0.0.24"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/powercontext_eval"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["live: requires Docker, network, Codex auth, and pinned upstream inputs"]

[tool.ruff]
target-version = "py311"
line-length = 120
```

Implement strict, frozen Pydantic models:

```python
class Arm(StrEnum):
    OFF = "off"
    ON = "on"


class PowerContextRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["latest", "branch", "tag", "commit"]
    value: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "PowerContextRef":
        if raw == "latest":
            return cls(kind="latest")
        kind, separator, value = raw.partition(":")
        if separator != ":" or kind not in {"branch", "tag", "commit"} or not value:
            raise ValueError("PowerContext ref must be latest or a typed branch:, tag:, or commit: ref")
        if kind == "commit" and re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
            raise ValueError("commit ref must contain a full 40-character hexadecimal SHA")
        return cls(kind=kind, value=value)
```

`EvaluationPaths` exposes `cache`, `work`, `runs`, `run_work`, `run_artifacts`, `arm_work()`, and
`arm_artifacts()` without creating directories in property accessors.

`cli.py` contains only a minimal Typer application and executable entry point:

```python
app = typer.Typer(no_args_is_help=True)


def main() -> None:
    app()
```

- [ ] **Step 4: Generate the nested lock and verify GREEN**

Run:

```bash
uv lock --project evaluation
uv run --project evaluation pytest evaluation/tests/unit/test_models.py evaluation/tests/unit/test_paths.py -q
```

Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add evaluation
git commit -m "feat(eval): scaffold independent runner"
```

### Task 2: Add bounded process execution and typed Git source resolution

**Files:**
- Create: `evaluation/src/powercontext_eval/process.py`
- Create: `evaluation/src/powercontext_eval/git_source.py`
- Create: `evaluation/tests/unit/test_process.py`
- Create: `evaluation/tests/contract/test_git_contract.py`

- [ ] **Step 1: Write failing tests for redaction, timeout, and one-time ref resolution**

```python
def test_command_failure_redacts_secret_values(tmp_path: Path) -> None:
    runner = ProcessRunner(redactions={"TOP-SECRET"})
    with pytest.raises(CommandFailed) as caught:
        runner.run(
            [sys.executable, "-c", "import sys; print('TOP-SECRET', file=sys.stderr); raise SystemExit(7)"],
            cwd=tmp_path,
        )
    assert "TOP-SECRET" not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_branch_is_resolved_to_one_full_sha_before_materialization(tmp_path: Path) -> None:
    remote, expected_sha = create_remote_with_branch(tmp_path, "main")
    source = GitSource(remote, cache_root=tmp_path / "cache", process=ProcessRunner())
    resolved = source.resolve(PowerContextRef.parse("branch:main"))
    source.materialize(resolved, tmp_path / "first")
    source.materialize(resolved, tmp_path / "second")
    assert resolved.sha == expected_sha
    assert head(tmp_path / "first") == expected_sha
    assert head(tmp_path / "second") == expected_sha
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --project evaluation pytest \
  evaluation/tests/unit/test_process.py \
  evaluation/tests/contract/test_git_contract.py -q
```

Expected: imports fail because process and Git adapters do not exist.

- [ ] **Step 3: Implement the minimal subprocess and Git adapters**

`ProcessRunner.run()` must:

- accept an argument sequence, explicit `cwd`, an environment allowlist/override, and a timeout;
- capture bytes without invoking a shell;
- run POSIX commands in a new session and terminate/reap both its process group and discoverable descendants that
  create a new session; every termination scan and post-kill pipe drain is bounded;
- raise typed `CommandTimedOut`, `CommandNotFound`, or `CommandFailed`;
- redact configured exact secret values from retained stdout, stderr, and exception text;
- automatically redact complete proxy URLs plus raw percent-encoded and decoded proxy userinfo inherited from the
  environment;
- never include the complete process environment in an error.

`GitSource` must:

- hash a credential-free normalized source string to choose its mirror cache;
- reject password/query/fragment credentials before creating a cache and require a Git credential helper;
- use command-scoped URL rewriting for username-only SSH/SCP transports so a raw transport is never persisted;
- redact both raw percent-encoded and decoded username components when Git or SSH echoes a partial transport;
- use only `clone --mirror`, `fetch --prune`, `ls-remote`, `rev-parse`, `clone --no-checkout`, and
  `checkout --detach`, plus `update-ref` for immutable pins and `config --get-all remote.origin.url` to validate
  existing mirrors;
- reject symlinked or out-of-root cache buckets before invoking Git;
- require an existing mirror to contain exactly one origin URL equal to the normalized source before fetching it;
- resolve `latest`, typed branch, typed tag, or full commit once into `ResolvedGitSource(source, requested, sha)`;
- pin each resolved SHA under `refs/powercontext-eval/pins/<sha>` so mirror refresh and GC cannot remove it;
- reject dirty local sources for `latest`;
- bind source, source-hash bucket, and immutable pin provenance before materializing;
- materialize only the already resolved full SHA through a unique temporary sibling and publish with an OS-level
  atomic no-replace rename; on Linux, use the libc `renameat2` wrapper when present and the architecture-specific
  raw `SYS_renameat2` syscall on older glibc such as m0's 2.17; fail closed when that primitive is unavailable;
- remove only the exact temporary materialization on failure so the requested target remains retryable;
- apply a finite timeout and non-interactive environment to every Git command;
- work with Git 1.8.3.1 and avoid `git -C`, worktrees, partial clone, or `switch`.

- [ ] **Step 4: Verify GREEN and compatibility**

Run:

```bash
uv run --project evaluation pytest \
  evaluation/tests/unit/test_process.py \
  evaluation/tests/contract/test_git_contract.py -q
git diff --check
```

Expected: tests pass and `git diff --check` exits zero.

- [ ] **Step 5: Commit Task 2**

```bash
git add evaluation/src/powercontext_eval/process.py \
  evaluation/src/powercontext_eval/git_source.py \
  evaluation/tests/unit/test_process.py \
  evaluation/tests/contract/test_git_contract.py
git commit -m "feat(eval): resolve immutable Git sources"
```

### Task 3: Add atomic artifacts, state transitions, secret scanning, and deterministic reports

**Files:**
- Create: `evaluation/src/powercontext_eval/artifacts.py`
- Create: `evaluation/src/powercontext_eval/report.py`
- Create: `evaluation/tests/unit/test_artifacts.py`
- Create: `evaluation/tests/unit/test_report.py`

- [ ] **Step 1: Write failing artifact and report tests**

```python
def test_artifact_store_writes_canonical_json_and_rejects_secrets(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, forbidden_values={"secret-token"})
    store.write_json("manifest.json", {"z": 1, "a": 2})
    assert (tmp_path / "manifest.json").read_bytes() == b'{\n  "a": 2,\n  "z": 1\n}\n'
    with pytest.raises(SecretDetected):
        store.write_text("bad.log", "authorization=secret-token")


def test_report_is_byte_deterministic(tmp_path: Path) -> None:
    bundle = completed_bundle_fixture(tmp_path)
    first = render_report(bundle)
    second = render_report(bundle)
    assert first == second
    assert "PowerContext OFF" in first
    assert "PowerContext ON" in first
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --project evaluation pytest \
  evaluation/tests/unit/test_artifacts.py \
  evaluation/tests/unit/test_report.py -q
```

Expected: imports fail because artifact and report modules do not exist.

- [ ] **Step 3: Implement atomic artifact and report primitives**

Use same-directory temporary files plus `Path.replace()` for atomic writes. JSON is UTF-8, sorted, indented by two,
and newline-terminated. Artifact relative paths reject absolute paths and `..`.

Arm state transitions are validated against:

```python
ALLOWED_TRANSITIONS = {
    "created": {"revisions_resolved", "configuration_error"},
    "revisions_resolved": {"gold_verified", "gold_check_failed", "infrastructure_error"},
    "gold_verified": {"environment_ready", "infrastructure_error"},
    "environment_ready": {"codex_running", "infrastructure_error"},
    "codex_running": {"patch_captured", "codex_error", "codex_timeout"},
    "patch_captured": {"evaluated", "evaluation_error"},
    "evaluated": {"treatment_validated", "invalid_treatment"},
    "treatment_validated": {"reported"},
}
```

`render_report()` reads only retained manifest, metrics, and treatment evidence; it does not query Git, Docker,
Codex, PowerContext, or the network.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run --project evaluation pytest \
  evaluation/tests/unit/test_artifacts.py \
  evaluation/tests/unit/test_report.py -q
```

Expected: all Task 3 tests pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add evaluation/src/powercontext_eval/artifacts.py \
  evaluation/src/powercontext_eval/report.py \
  evaluation/tests/unit/test_artifacts.py \
  evaluation/tests/unit/test_report.py
git commit -m "feat(eval): record auditable run artifacts"
```

### Task 4: Implement the Codex and PowerContext arm contract

**Files:**
- Create: `evaluation/src/powercontext_eval/codex.py`
- Create: `evaluation/src/powercontext_eval/powercontext_sut.py`
- Create: `evaluation/tests/contract/fixtures/fake_codex.py`
- Create: `evaluation/tests/contract/test_codex_contract.py`

- [ ] **Step 1: Write failing OFF/ON argument and treatment-evidence tests**

```python
def test_off_and_on_commands_differ_only_by_plugin_switch(base_invocation: CodexInvocation) -> None:
    off = base_invocation.argv(Arm.OFF)
    on = base_invocation.argv(Arm.ON)
    assert without_plugin_switch(off) == without_plugin_switch(on)
    assert "--disable" in off and off[off.index("--disable") + 1] == "plugins"
    assert "--enable" in on and on[on.index("--enable") + 1] == "plugins"
    assert "--ignore-user-config" not in off


def test_treatment_validation_fails_closed_without_on_prompt_source() -> None:
    with pytest.raises(InvalidTreatment):
        validate_treatment(Arm.ON, TreatmentEvidence(plugin_installed=True, server_ready=True, prompt_sources=0))


def test_off_rejects_any_powercontext_activity() -> None:
    with pytest.raises(InvalidTreatment):
        validate_treatment(Arm.OFF, TreatmentEvidence(plugin_installed=True, server_ready=True, prompt_sources=1))
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run --project evaluation pytest evaluation/tests/contract/test_codex_contract.py -q
```

Expected: imports fail because Codex and PowerContext SUT adapters do not exist.

- [ ] **Step 3: Implement isolated Codex and SUT lifecycle**

The Codex adapter must construct:

```text
codex exec --ephemeral --ignore-rules --json
  --disable shell_snapshot
  --dangerously-bypass-approvals-and-sandbox
  --dangerously-bypass-hook-trust
  --model gpt-5.6-sol
  -c model_reasoning_effort="medium"
  --enable|--disable plugins
  -C /workspace
  -
```

It streams stdout to `codex-events.jsonl`, stderr to a separate redacted log, writes the last message, parses usage,
and treats timeout/nonzero exit as infrastructure outcomes rather than resolved status. Pin Codex CLI `0.145.0`
for the first paid experiment. Reject any attempt to use the dangerous bypass on the `m0` host: it is valid only
inside the exact disposable task container, where the standalone CLI does not depend on a host `bwrap` helper.

The SUT adapter must:

- materialize the resolved source into disposable arm work;
- create a run-owned Docker internal bridge and an exactly tracked host relay bound only to its gateway, forwarding
  to the configured loopback proxy;
- give both arms the same relay URL and no direct network path, host-network mode, proxy configuration file, or
  Docker socket;
- create an arm-specific package environment and PowerContext data directory;
- install the local marketplace and only `powercontext@powercontext` into the isolated Codex home;
- start `powercontext server run` at loopback port 8000 in the same arm container;
- wait for liveness/readiness;
- set `POWERCONTEXT_CODEX_SCOPE_ID=eval:<run-id>:<arm>`;
- query retained Server evidence for the exact scope after Codex exits;
- terminate the Server by exact process/container identity;
- fail ON when its prompt Source is absent and fail OFF when any PowerContext activity exists.

- [ ] **Step 4: Verify the fake contract and one real local Codex plugin smoke**

Run:

```bash
uv run --project evaluation pytest evaluation/tests/contract/test_codex_contract.py -q
uv run --project evaluation python -m powercontext_eval.cli codex-contract-smoke \
  --codex-bin /Applications/ChatGPT.app/Contents/Resources/codex \
  --powercontext-source .
```

Expected: fake tests pass; the real smoke reports OFF activity `0`, ON prompt Sources `>=1`, and contains no
credential value in its artifact directory. The real smoke uses a harmless prompt and a disposable local Server.

- [ ] **Step 5: Commit Task 4**

```bash
git add evaluation/src/powercontext_eval/codex.py \
  evaluation/src/powercontext_eval/powercontext_sut.py \
  evaluation/tests/contract
git commit -m "feat(eval): isolate Codex PowerContext treatments"
```

### Task 5: Implement the pinned SWE-bench Pro prediction and official evaluator adapter

**Files:**
- Create: `evaluation/src/powercontext_eval/benchmarks/__init__.py`
- Create: `evaluation/src/powercontext_eval/benchmarks/base.py`
- Create: `evaluation/src/powercontext_eval/benchmarks/swebench_pro/__init__.py`
- Create: `evaluation/src/powercontext_eval/benchmarks/swebench_pro/adapter.py`
- Create: `evaluation/src/powercontext_eval/benchmarks/swebench_pro/prediction.py`
- Create: `evaluation/src/powercontext_eval/benchmarks/swebench_pro/evaluator.py`
- Create: `evaluation/tests/contract/fixtures/fake_evaluator.py`
- Create: `evaluation/tests/contract/test_swebench_contract.py`

- [ ] **Step 1: Write failing schema, prompt-boundary, and Gold gate tests**

```python
def test_prediction_is_official_json_array_and_preserves_patch_bytes() -> None:
    patch = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n"
    encoded = encode_predictions("instance_x", patch, "codex-0.145.0")
    assert json.loads(encoded) == [{"instance_id": "instance_x", "patch": patch, "prefix": "codex-0.145.0"}]


def test_prompt_excludes_gold_and_hidden_test_fields(instance: SweBenchProInstance) -> None:
    prompt = instance.codex_prompt()
    assert instance.problem_statement in prompt
    assert instance.requirements in prompt
    assert instance.interface in prompt
    assert instance.patch not in prompt
    assert instance.test_patch not in prompt


def test_gold_failure_prevents_arm_factory_from_being_called() -> None:
    arms = Mock()
    with pytest.raises(GoldCheckFailed):
        run_after_gold(GoldResult(resolved=False), arms)
    arms.assert_not_called()
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run --project evaluation pytest evaluation/tests/contract/test_swebench_contract.py -q
```

Expected: imports fail because the benchmark adapter does not exist.

- [ ] **Step 3: Implement the exact pinned official adapter**

The adapter defaults to the fixed harness and dataset revisions. It:

- downloads one dataset raw record through an external pinned preparation command and stores it as one-line JSONL;
- validates the required 17 dataset fields;
- renders a Codex prompt from only `problem_statement`, `requirements`, and `interface`;
- records `repo`, `base_commit`, `dockerhub_tag`, and Docker manifest digest;
- rejects `GIT binary patch` and `Binary files ... differ`;
- serializes a JSON array with exactly `instance_id`, `patch`, and `prefix`;
- invokes `swe_bench_pro_eval.py` from the harness root with `--num_workers 1`, `--use_local_docker`,
  `--docker_platform linux/amd64`, `--redo`, and initially `--block_network`;
- parses `eval_results.json` and requires an exact boolean result for the instance;
- retains raw evaluator output and never implements its own pass/fail logic.

- [ ] **Step 4: Verify GREEN with the fake evaluator**

Run:

```bash
uv run --project evaluation pytest evaluation/tests/contract/test_swebench_contract.py -q
```

Expected: all contract tests pass, including missing-result and binary-patch failures.

- [ ] **Step 5: Commit Task 5**

```bash
git add evaluation/src/powercontext_eval/benchmarks evaluation/tests/contract
git commit -m "feat(eval): adapt official SWE-bench Pro evaluator"
```

### Task 6: Orchestrate runs, diagnostics, CLI, and deterministic comparison

**Files:**
- Create: `evaluation/src/powercontext_eval/doctor.py`
- Create: `evaluation/src/powercontext_eval/runner.py`
- Modify: `evaluation/src/powercontext_eval/cli.py`
- Create: `evaluation/tests/unit/test_runner.py`
- Create: `evaluation/tests/e2e/test_smoke.py`

- [ ] **Step 1: Write failing orchestration and CLI tests**

```python
def test_both_resolves_once_runs_gold_then_clean_off_and_on(orchestrator_fixture) -> None:
    result = orchestrator_fixture.run(powercontext="both")
    assert orchestrator_fixture.resolve_calls == 1
    assert orchestrator_fixture.events == ["resolve", "gold", "off", "evaluate-off", "on", "evaluate-on", "report"]
    assert result.off.workspace != result.on.workspace


def test_doctor_json_exits_nonzero_when_auth_is_missing(monkeypatch) -> None:
    result = CliRunner().invoke(app, ["doctor", "--json", "--root", "/tmp/eval"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["checks"]["codex_auth"]["ok"] is False
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run --project evaluation pytest \
  evaluation/tests/unit/test_runner.py \
  evaluation/tests/e2e/test_smoke.py -q
```

Expected: runner and doctor imports or commands are missing.

- [ ] **Step 3: Implement orchestration and public CLI**

`doctor` checks:

- root filesystem permissions and free space;
- loopback proxy reachability without credentials;
- Docker client/server and image-pull capability;
- Git conservative-command compatibility;
- Python 3.11+, uv, and pinned Codex executable;
- Codex login status without printing auth;
- harness checkout and exact SHA;
- dataset raw instance availability;
- PowerContext source/ref resolvability.

`runner` resolves immutable inputs, writes the manifest, runs Gold, runs requested arms serially, captures patches
with `git diff --binary --full-index <base_commit> --`, grades each prediction in a clean evaluator environment,
validates treatment evidence, scans artifacts for forbidden values, and renders the report twice to verify byte
determinism.

The CLI exposes:

```text
powercontext-eval doctor
powercontext-eval codex-contract-smoke
powercontext-eval swebench-pro gold-check
powercontext-eval swebench-pro run
powercontext-eval report
```

- [ ] **Step 4: Run all nested tests**

Run:

```bash
uv lock --project evaluation --locked
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests -m "not live" -q
uv run --project evaluation ruff check evaluation
uv run --project evaluation ruff format --check evaluation
uv run --directory evaluation ty check src tests
```

Expected: every command exits zero.

- [ ] **Step 5: Commit Task 6**

```bash
git add evaluation
git commit -m "feat(eval): orchestrate reproducible A/B runs"
```

### Task 7: Integrate repository checks and document m0 operation

**Files:**
- Modify: `Makefile`
- Create: `.github/workflows/evaluation.yml`
- Create: `evaluation/README.md`
- Create: `evaluation/deploy/m0/README.md`
- Create: `evaluation/deploy/m0/powercontext-eval.env.example`

- [ ] **Step 1: Write the expected Make/CI contract**

The Makefile must expose:

```make
.PHONY: eval-lock-check
eval-lock-check:
	@uv lock --project evaluation --locked

.PHONY: eval-test
eval-test:
	@uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests -m "not live"

.PHONY: eval-check
eval-check: eval-lock-check eval-test
	@uv run --project evaluation ruff check evaluation
	@uv run --project evaluation ruff format --check evaluation
	@uv run --directory evaluation ty check src tests
```

The workflow runs `make eval-check` on Python 3.11 without Docker credentials or live tests.

- [ ] **Step 2: Add redaction-safe deployment documentation**

Document exact `m0` directories, proxy variables, tool installation, `doctor`, Gold Patch, OFF/ON, report, and
cleanup commands. The env example contains names and non-secret defaults only:

```dotenv
POWERCONTEXT_EVAL_ROOT=/data/powercontext-eval
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
NO_PROXY=127.0.0.1,localhost
```

Do not include Mihomo configuration, Codex auth content, tokens, or subscription URLs.

- [ ] **Step 3: Run root and nested verification**

Run:

```bash
make eval-check
make test
make check
make docs-test
git diff --check
```

Expected: every command exits zero. Any unrelated pre-existing failure is recorded precisely and not hidden.

- [ ] **Step 4: Commit Task 7**

```bash
git add Makefile .github/workflows/evaluation.yml evaluation
git commit -m "ci(eval): verify runner independently"
```

### Task 8: Deploy the pinned stack to m0 and run the minimal live loop

**Files:**
- No repository source files beyond Task 7
- Produce on m0: `/data/powercontext-eval/runs/<run-id>/...`

- [ ] **Step 1: Verify proxy and protect existing Docker workloads**

Run read-only checks:

```bash
ssh m0 'systemctl is-active mihomo && systemctl is-enabled mihomo'
ssh m0 "ss -lntup | awk '\\$5 ~ /:7890$/ {print \\$5}'"
ssh m0 'docker ps --format "{{.Names}} {{.Status}}"'
```

Expected: Mihomo is active/enabled, only loopback port 7890 is present, and the three existing containers remain
running.

- [ ] **Step 2: Install the isolated toolchain under `/data`**

Use the official standalone `uv` installer with `UV_INSTALL_DIR=/data/powercontext-eval/bin`, then use
`uv python install 3.12` and `uv sync --project <deployed-checkout>/evaluation --locked`. Install a pinned official
Linux x86-64 Codex standalone release in `/data/powercontext-eval/codex/<version>/codex` and symlink it from
`/data/powercontext-eval/bin/codex`; verify its published checksum before activation. This avoids changing the
system Python/Git and avoids a Node runtime whose current builds are incompatible with the host's glibc 2.17.

Clone the harness to `/data/powercontext-eval/cache/swebench-pro.git`, check out the full pinned SHA, initialize its
pinned submodules, and create a dedicated harness venv from its requirements. Route these downloads through
`http://127.0.0.1:7890`. Do not configure or restart Docker.

Verify:

```bash
ssh m0 '/data/powercontext-eval/bin/uv --version'
ssh m0 '/data/powercontext-eval/bin/codex --version'
ssh m0 '/data/powercontext-eval/venv/bin/python --version'
```

Expected: all commands exit zero and their versions are written to `environment.json`.

- [ ] **Step 3: Resolve Docker image delivery without interrupting current containers**

Use the already available trusted amd64 Docker engine outside `m0` to pull
`jefzda/sweap-images:flipt-io.flipt-flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9`, verify manifest digest
`sha256:d2c9d5460c479cb257a0588a603021f4e83e31f2614146728336689854f52803`, export it with `docker save`,
transfer the archive through an owner-only temporary path, and import it with `docker load` on `m0`. Verify the
loaded image ID/digest before deleting the exact temporary archive. Do not reconfigure or restart the existing
Docker daemon.

- [ ] **Step 4: Complete Codex authentication and run doctor**

Use Codex device/browser login when interactive authorization is required. Never print or copy the auth file into
the result tree.

Run:

```bash
ssh m0 '/data/powercontext-eval/bin/powercontext-eval doctor \
  --root /data/powercontext-eval \
  --proxy http://127.0.0.1:7890 \
  --json'
```

Expected: top-level `"ok": true`.

- [ ] **Step 5: Run the Gold Patch gate**

```bash
ssh m0 '/data/powercontext-eval/bin/powercontext-eval swebench-pro gold-check \
  --instance-id instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9 \
  --harness-ref ca10a60a5fcae51e6948ffe1485d4153d421e6c5 \
  --dataset-ref 7ab5114912baf22bb098818e604c02fe7ad2c11f \
  --root /data/powercontext-eval'
```

Expected: official `eval_results.json` maps the exact instance ID to `true`.

- [ ] **Step 6: Run one clean OFF/ON pair and generate the report**

```bash
ssh m0 '/data/powercontext-eval/bin/powercontext-eval swebench-pro run \
  --instance-id instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9 \
  --powercontext both \
  --powercontext-source https://github.com/oceanbase/powercontext.git \
  --powercontext-ref latest \
  --harness-ref ca10a60a5fcae51e6948ffe1485d4153d421e6c5 \
  --dataset-ref 7ab5114912baf22bb098818e604c02fe7ad2c11f \
  --root /data/powercontext-eval'
```

Expected: both arms reach an official resolved/unresolved benchmark outcome, treatment evidence is valid, and
`report.md` exists. The framework may pass even when either agent patch is officially unresolved.

- [ ] **Step 7: Audit retained evidence and existing services**

Run the artifact secret scan, regenerate the report and compare bytes, verify every manifest SHA/version against
the live inputs, and confirm all three pre-existing Docker containers remain running.

### Task 9: Final review and completion audit

**Files:**
- Review all files changed since `66710ee`

- [ ] **Step 1: Run a fresh specification compliance review**

Compare every section of
`docs/superpowers/specs/2026-07-28-codex-swebench-pro-evaluation-runner-design.md` to implementation and retained
live evidence. Fix every missing or extra behavior through a failing test first.

- [ ] **Step 2: Run a fresh code-quality and security review**

Review process boundaries, argument injection, path traversal, cleanup targeting, secret redaction, Docker
isolation, treatment validity, and deterministic artifacts. Fix each confirmed issue through TDD.

- [ ] **Step 3: Run final verification**

```bash
make test
make check
make docs-test
make eval-check
git diff --check 66710ee..HEAD
git status --short
```

On `m0`, verify the Gold result, both official evaluator results, treatment evidence, report determinism, secret
scan, and existing container health from current artifacts.

- [ ] **Step 4: Record the final commit**

```bash
git add .
git commit -m "test(eval): verify Codex SWE-bench Pro loop"
```

Create the commit only when the final audit produced necessary tracked changes; otherwise retain the already
verified task commits without an empty commit.
