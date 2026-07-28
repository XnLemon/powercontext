# Codex SWE-bench Pro Evaluation Runner Design

**Status:** Draft for implementation  
**Date:** 2026-07-28  
**Scope:** Minimum reproducible evaluation loop for Codex with PowerContext disabled and enabled

## 1. Purpose

PowerContext needs a repeatable way to determine whether a product change produces a positive outcome for an
agent doing real software-engineering work. The first evaluation target is Codex running public SWE-bench Pro
tasks. The runner must fetch an explicitly selected PowerContext revision, execute a fair OFF/ON comparison, use
the official evaluator, and retain enough evidence to reproduce and audit the result.

This is an evaluation system for PowerContext, not a benchmark embedded in the PowerContext runtime. The runner
therefore has its own version and checkout. A run may evaluate any PowerContext commit, branch, tag, or the current
remote head without replacing the runner's own source.

## 2. MVP success criteria

The first end-to-end milestone is complete only when all of the following are true on `m0`:

1. A diagnostic command proves that the required proxy, Docker daemon, Codex CLI, Codex authentication,
   PowerContext source, SWE-bench Pro harness, and result directory are usable.
2. The official SWE-bench Pro evaluator accepts a selected task's Gold Patch and reports the expected resolved
   result. A task whose Gold Patch cannot pass is rejected before any paid agent run.
3. The same selected task is run once with PowerContext OFF and once with PowerContext ON from independent,
   clean task workspaces.
4. Both arms produce a patch artifact, a prediction accepted by the official evaluator, evaluator output, Codex
   JSONL events, environment provenance, metrics, and a Markdown comparison report. An empty or incorrect patch
   is a valid agent result as long as the infrastructure records and evaluates it correctly.
5. The OFF arm proves that PowerContext was not loaded or invoked. The ON arm proves that the requested
   PowerContext revision was loaded, its Server was ready, and at least the prompt hook completed against that
   Server. An ON arm that cannot establish those facts fails closed and is not reported as a valid treatment run.
6. No Codex credential, proxy configuration, token, subscription URL, or other secret appears in the repository
   or result bundle.

The first OFF/ON pair is an engineering smoke test. It does not establish a statistically meaningful product
effect and must not be described as a release-quality benchmark score.

## 3. Non-goals

The MVP does not:

- define a PowerContext-specific benchmark;
- run the complete SWE-bench Pro public split;
- force Codex into an artificial two-stage handoff;
- add compaction, subagent, or lifecycle hooks to the PowerContext plugin;
- change Codex's native planning, compaction, or subagent behavior;
- compare multiple models;
- claim product improvement from one nondeterministic OFF/ON pair;
- replace the official SWE-bench Pro evaluator;
- expose the existing `m0` Docker workloads to task code;
- publish artifacts or results to an external service.

## 4. Architecture

The repository contains an independent nested `uv` project at `evaluation/`. It is not included in the
`powercontext` wheel and does not import the runner from the PowerContext revision under test.

```text
powercontext runner checkout
└── evaluation/
    ├── pyproject.toml
    ├── src/powercontext_eval/
    └── tests/

/data/powercontext-eval/
├── cache/
│   ├── git/<normalized-source-hash>/
│   ├── swebench-pro.git/
│   └── dataset/
├── codex/
│   ├── auth/
│   └── distributions/
├── work/<run-id>/<arm>/
│   ├── codex-home/
│   ├── powercontext-home/
│   └── repo/
└── runs/
```

For each run, the runner resolves all moving references to immutable identifiers before task execution:

- runner Git SHA;
- requested and resolved PowerContext Git ref;
- PowerContext plugin manifest version;
- Codex CLI version;
- Codex model and reasoning level;
- SWE-bench Pro harness Git SHA;
- dataset revision;
- task instance ID and task image identifier;
- invocation budgets and timeouts.

The runner prepares one task environment and then creates two clean arms from the same immutable base:

```text
Run specification
  -> resolve and record revisions
  -> Gold Patch evaluator gate
  -> OFF clean task environment
  -> ON clean task environment
  -> official evaluation for each prediction
  -> comparison report
```

OFF and ON execute serially in the MVP so that they do not contend for CPU, memory, Docker, PowerContext's
loopback port, or Codex account limits.

## 5. Component boundaries

The nested project uses small modules with explicit responsibilities:

- `models.py`: immutable run specification, resolved provenance, arm state, metrics, and artifact path models.
- `process.py`: bounded subprocess execution, environment filtering, structured command results, and redaction-safe
  error messages.
- `git_source.py`: source-keyed mirror/cache management and safe resolution of typed commit, tag, branch, and
  `latest` references into a full SHA.
- `codex.py`: Codex version/auth diagnostics, isolated `CODEX_HOME` preparation, command construction, JSONL
  capture, and terminal outcome extraction.
- `powercontext_sut.py`: source checkout, plugin installation into the isolated Codex home, Server lifecycle,
  readiness, prompt-hook evidence, and treatment validation.
- `benchmarks/swebench_pro/adapter.py`: pinned harness/dataset checkout, task metadata access, Gold Patch
  self-check, task environment lifecycle, and clean evaluator handoff.
- `benchmarks/swebench_pro/prediction.py`: exact official prediction array serialization without patch mutation.
- `benchmarks/swebench_pro/evaluator.py`: official evaluator invocation and normalized result parsing.
- `artifacts.py`: atomic creation of run directories and canonical JSON/JSONL/text artifacts.
- `report.py`: deterministic Markdown comparison report generated exclusively from recorded artifacts.
- `doctor.py`: non-mutating readiness checks with machine-readable results.
- `runner.py`: fail-fast orchestration state machine and cleanup.
- `cli.py`: Typer commands and exit-code mapping; it contains no benchmark logic.

External commands are injected behind narrow protocol interfaces in unit tests. Tests validate observable command
arguments, state transitions, artifacts, and failure behavior rather than private call counts.

On POSIX, a timeout terminates and reaps the complete child process group, not only the direct child. Networked Git
commands have a finite timeout and disable terminal/credential-manager prompts.

## 6. CLI contract

The nested project installs one executable:

```text
powercontext-eval
```

### 6.1 Diagnostics

```bash
powercontext-eval doctor \
  --root /data/powercontext-eval \
  --codex-bin /data/powercontext-eval/codex/bin/codex \
  --proxy http://127.0.0.1:7890 \
  --json
```

Exit code is zero only when every required check passes. Diagnostic output names the failed capability without
printing secret values.

### 6.2 Gold Patch gate

```bash
powercontext-eval swebench-pro gold-check \
  --instance-id <instance-id> \
  --harness-ref <full-sha> \
  --dataset-ref <full-sha> \
  --root /data/powercontext-eval
```

The command writes an ordinary result bundle with `kind=gold-check`. It must use the same official evaluator path
that agent predictions use.

### 6.3 OFF/ON run

```bash
powercontext-eval swebench-pro run \
  --instance-id <instance-id> \
  --powercontext both \
  --powercontext-source https://github.com/oceanbase/powercontext.git \
  --powercontext-ref <latest-or-typed-ref> \
  --harness-ref <full-sha> \
  --dataset-ref <full-sha> \
  --codex-bin /data/powercontext-eval/codex/bin/codex \
  --model <model-id> \
  --reasoning <level> \
  --wall-timeout-seconds <seconds> \
  --root /data/powercontext-eval
```

`--powercontext` accepts `off`, `on`, or `both`. The comparison report is generated only for `both`.
`--powercontext-ref` accepts exactly one of:

- `latest`, meaning the remote's symbolic HEAD, resolved once before either arm;
- `commit:<40-hex-sha>`;
- `branch:<branch-name>`;
- `tag:<tag-name>`.

Local sources use `latest` to mean their clean current `HEAD`. An untyped ref is rejected so that a branch and tag
with the same name cannot be resolved accidentally.

The run command never treats `latest` as provenance. It resolves it to a full Git SHA and records both the
requested text and resolved SHA before launching an arm.

Resolving a commit also creates an immutable private ref in the source mirror so a later branch move, fetch,
reflog expiry, or garbage collection cannot remove an object needed by either arm. Materialization builds in a
unique sibling temporary directory and publishes only a fully verified detached checkout; failed attempts leave
the public target absent and retryable.

## 7. Codex isolation and treatment

Each arm receives a fresh, disposable directory outside the retained run bundle:

```text
/data/powercontext-eval/work/<run-id>/<off-or-on>/codex-home/
```

Authentication is supplied from an operator-managed source outside the result tree. The runner may copy only the
minimum Codex auth material into the ephemeral home with owner-only permissions. It must never include that home
in retained artifacts. Logs pass through exact-value redaction for any secret-bearing environment variables known
to the runner.

Credential-bearing proxy URLs are automatically treated as redaction inputs. Embedded Git passwords, query
credentials, and fragments are rejected before cache creation; authenticated Git sources must use an external
credential helper. Username-only SSH/SCP transports may use a command-scoped Git URL rewrite, but the cached
mirror origin and provenance always contain only the sanitized source.

Both homes receive the same pinned Codex CLI, authentication, model, reasoning level, prompt, task repository,
shell environment policy, sandbox mode, time limit, and resource budget. The first paid experiment pins Codex CLI
`0.145.0`, model `gpt-5.6-sol`, and reasoning effort `medium`; changing any of them creates a new experiment
configuration rather than silently changing an existing run. The only treatment switch is the stable Codex
`plugins` feature:

- OFF passes `--disable plugins`.
- ON passes `--enable plugins` and `--dangerously-bypass-hook-trust`.

The isolated Codex environment contains no marketplace or plugin other than the resolved PowerContext checkout.
This makes the global feature switch equivalent to a PowerContext switch.

Both arms use:

```text
codex exec
  --ephemeral
  --ignore-rules
  --json
  --disable shell_snapshot
  --dangerously-bypass-approvals-and-sandbox
  --cd <task-workspace>
  --model gpt-5.6-sol
  -c model_reasoning_effort="medium"
```

The dangerous Codex execution flag is permitted only inside the dedicated, disposable SWE-bench task container.
It is never used directly against the `m0` host workspace. The pinned standalone Codex binary has no adjacent
`bwrap` helper on `m0`, so a host-side tool-using smoke is not a valid substitute for the container contract.
`shell_snapshot` is explicitly disabled in both arms because `m0`'s login-shell replay is not part of the
benchmark treatment and produced a validation error in the no-tool authentication smoke.

The runner deliberately does not pass `--ignore-user-config`: Codex documents that flag as ignoring the isolated
`CODEX_HOME/config.toml`, which also contains this run's marketplace and installed-plugin selection. The home is
already generated from scratch and contains no operator preferences. A real-Codex contract smoke must prove that
the ON invocation loads the installed plugin and that `--disable plugins` suppresses it before the first paid
benchmark arm is accepted.

The task prompt is taken from the official instance problem statement plus a small stable instruction to solve the
issue, run relevant tests, and leave the final code changes in the Git working tree. The prompt must not mention
the treatment arm.

## 8. PowerContext treatment lifecycle

The resolved PowerContext revision is checked out once into a read-only source cache and copied into each arm's
disposable environment. The same source is present in both arms.

Before Codex starts:

1. Install the resolved PowerContext package and Codex plugin dependencies.
2. Install only that checkout's marketplace/plugin into the isolated Codex home.
3. Create a new empty PowerContext data directory for the arm.
4. Start the PowerContext Server at `127.0.0.1:8000`.
5. Wait for liveness and readiness.

Codex, its plugin Hook, and the PowerContext Server run inside the same disposable arm container/network namespace.
This is required because the current plugin endpoint is fixed to loopback. The MVP executes arms serially, so both
can use port 8000 without rewriting the pristine plugin or its `.mcp.json`.

The Server is started for both arms to keep the process topology stable. In OFF, plugins are globally disabled and
the Server must receive no hook or MCP activity. In ON, plugins are globally enabled. Both arms set an explicit
`POWERCONTEXT_CODEX_SCOPE_ID=eval:<run-id>:<arm>` so that hook evidence cannot be confused with another run.

Treatment validation is evidence-based:

- plugin list before execution identifies the installed PowerContext plugin and its version;
- Server liveness/readiness succeeds;
- ON receives the prompt Source created by the `UserPromptSubmit` hook for the current run scope;
- OFF has no prompt Source and no PowerContext MCP invocation;
- the plugin checkout SHA matches the manifest.

The current plugin is fail-open by product design. The evaluation runner adds a post-run fail-closed validity gate:
if the ON hook evidence is absent, the arm is marked `invalid_treatment`, regardless of patch quality.

## 9. SWE-bench Pro adapter

The adapter pins, clones, and invokes the public official harness rather than copying its grading logic. Exact
commands and schema fields are recorded from the pinned harness version in an adapter contract test.

The MVP defaults are immutable:

- harness repository: `https://github.com/scaleapi/SWE-bench_Pro-os.git`;
- harness commit: `ca10a60a5fcae51e6948ffe1485d4153d421e6c5`;
- dataset: `ScaleAI/SWE-bench_Pro`;
- dataset revision: `7ab5114912baf22bb098818e604c02fe7ad2c11f`;
- split: `test`;
- official local evaluator entry point: `swe_bench_pro_eval.py`.

The runner records the harness submodule commits and Docker image manifest digest as additional provenance because
an image tag is mutable even when the harness and dataset are pinned.

Required adapter behavior:

1. Load one instance by exact ID from the pinned public dataset.
2. Record the problem statement, requirements, interface, repository, base commit, task image identifier, Gold
   Patch, and evaluator inputs needed by the pinned harness.
3. Prepare a clean disposable task container using the official environment definition.
4. Run Codex inside that container and extract `git diff --binary` relative to the official base state.
5. Write the patch in the official prediction JSON-array schema without normalizing or truncating it.
6. Run the official evaluator for only that instance.
7. Parse the official result into a small normalized metrics object while retaining raw evaluator output.

The Gold Patch command uses the dataset's official patch value with the same prediction and evaluator path. A
candidate smoke instance is eligible only after its Gold Patch resolves in this exact environment.

Only `problem_statement`, `requirements`, and `interface` are exposed to Codex. The Gold Patch, test patch,
fail-to-pass/pass-to-pass lists, selected test files, run scripts, and evaluator output remain in the trusted
controller/evaluator boundary.

The evaluator runs in a fresh, credential-free environment after the Codex editing container is destroyed. It
receives the official one-line raw JSONL instance and prediction array:

```json
[
  {
    "instance_id": "instance_...",
    "patch": "diff --git ...",
    "prefix": "codex-<version>"
  }
]
```

Binary patches are rejected before evaluation because the pinned official evaluator strips binary hunks.

The adapter must not silently fall back to a locally invented scoring rule. An incompatible harness revision,
missing image, unexpected prediction schema, or missing official result is a hard infrastructure failure.

## 10. Run state machine and failure semantics

The orchestrator persists the manifest before any paid model call and updates arm status atomically:

```text
created
  -> revisions_resolved
  -> gold_verified
  -> environment_ready
  -> codex_running
  -> patch_captured
  -> evaluated
  -> treatment_validated
  -> reported
```

Terminal failure classes are distinct:

- `configuration_error`
- `infrastructure_error`
- `gold_check_failed`
- `codex_error`
- `codex_timeout`
- `invalid_treatment`
- `evaluation_error`
- `agent_unresolved`
- `agent_resolved`

`agent_unresolved` and `agent_resolved` are successful infrastructure outcomes. They describe benchmark quality,
not runner health.

An interrupted run retains completed evidence and can be inspected, but the MVP does not resume a partially
executed Codex arm. Re-running creates a new run ID and clean environment.

## 11. Artifact contract

Each run is immutable after terminal reporting:

```text
runs/<run-id>/
├── run-manifest.json
├── environment.json
├── gold/
│   ├── prediction.json
│   ├── evaluator.log
│   └── metrics.json
├── arms/
│   ├── off/
│   │   ├── state.json
│   │   ├── codex-events.jsonl
│   │   ├── codex-stderr.log
│   │   ├── final-message.txt
│   │   ├── patch.diff
│   │   ├── prediction.json
│   │   ├── evaluator.log
│   │   ├── metrics.json
│   │   └── treatment-evidence.json
│   └── on/
│       └── ...
└── report.md
```

`run-manifest.json` is canonical JSON and contains:

- run ID and timestamps;
- requested configuration;
- all resolved revisions and versions;
- immutable task identity;
- arm order;
- budgets and timeouts;
- artifact-relative paths;
- final infrastructure and benchmark outcomes.

`environment.json` includes OS, architecture, Docker, Python, uv, Codex, and proxy reachability metadata, but no
full environment dump.

`metrics.json` includes official resolved status, elapsed wall time, patch byte/line counts, Codex usage fields
available from JSONL, and native compaction/subagent/tool events when present. Missing optional telemetry is
represented as unavailable, never inferred.

The report is reproducible from the manifest and retained arm artifacts without querying live services.
Neither `codex-home` nor `powercontext-home` is copied from `work/` into `runs/`.

## 12. m0 deployment and safety

The dedicated root is `/data/powercontext-eval`; the small root filesystem is not used for caches, task workspaces,
or result bundles.

Mihomo is installed as a systemd service running as the existing `rongfeng.frf` user. Its configuration is copied
from `dev` through a controller-owned mode-0700 temporary directory and is never printed or committed. It binds
only `127.0.0.1:7890`.

Task containers do not use host networking and cannot reach that loopback listener directly. For each run, the
runner creates a uniquely named Docker `--internal` bridge, reads that bridge's gateway address, and starts an
exactly tracked host `socat` relay bound only to that gateway. The relay forwards to `127.0.0.1:7890`; task
containers receive only the relay URL through their proxy environment. This gives Codex proxy-only egress without
mounting Mihomo configuration, publishing a host port, exposing the relay on a LAN interface, or granting direct
container Internet access. OFF and ON use the same bridge, relay, and proxy environment. The relay and bridge are
removed by exact run identity after both arms, including failure paths.

The existing Docker daemon currently hosts unrelated workloads. The evaluation deployment must:

- inspect existing restart policies and daemon configuration before any change;
- never remove, stop, recreate, or attach task networks to existing containers;
- not restart or reload Docker without a separate operator approval;
- prefer an already configured safe daemon proxy or controlled offline image import when a restart is not
  approved;
- keep evaluation containers, networks, labels, and names under a unique `powercontext-eval` prefix.

The installed system Git on `m0` is 1.8.3.1. The runner therefore uses the conservative
`clone --mirror`/`fetch`/`rev-parse`/`checkout --detach` command family and subprocess `cwd`, not worktrees,
partial clones, `switch`, or `git -C`. A future deployment may pin a newer Git, but the MVP does not require
replacing the host Git.

Mirror buckets are direct non-symlink children of the configured cache root. A pre-existing symlink or path that
resolves outside that root is rejected before Git executes.

The current MVP copies only the Mac Codex `auth.json` into a dedicated mode-0600 m0 auth directory and verifies
`codex login status` there. Credentials remain outside the repository and retained run tree; no Codex config,
history, sessions, or shell state is copied.

## 13. Security model

SWE-bench task repositories and tests are treated as untrusted. The Codex editing task executes in a disposable
container with:

- no host Docker socket;
- no host home-directory mount;
- no access to the PowerContext runner checkout;
- a dedicated writable workspace;
- the minimum Codex auth material required for the run, available only for the Codex phase;
- no proxy configuration file;
- no unrelated environment secrets;
- only proxy-mediated egress through a run-owned relay on a dedicated internal Docker bridge;
- resource and wall-clock limits;
- cleanup by exact run/container identifiers.

The first smoke task must be manually audited before a credential is made available in its container. Copying a
Codex login file into a task container remains an acknowledged MVP risk even when the container is disposable.
The preferred production design is a narrow Responses API credential-injection proxy plus a dedicated,
short-lived, quota-limited evaluation account. The proxy protects the long-lived credential but does not by itself
prevent quota abuse, so task egress, time, and spend also require limits.

The official evaluator runs separately with no Codex credential, no host Docker socket, and network disabled when
the Gold Patch proves that the image contains all dependencies.

## 14. Test strategy

Implementation follows red-green-refactor TDD.

Unit tests cover:

- safe ref normalization and immutable SHA resolution;
- rejection of ambiguous/missing refs;
- subprocess timeouts and redacted failures;
- exact OFF/ON Codex argument differences;
- isolated home permissions and artifact exclusion;
- ON fail-closed and OFF no-activity treatment validation;
- prediction serialization without patch mutation;
- rejection of binary patches;
- official result parsing and missing-result failures;
- atomic state/manifest writes;
- deterministic reports;
- secret-pattern absence in artifacts.

Contract tests use small fake executables and fixture repositories to validate command-line and filesystem
behavior without network access.

Integration tests invoke the pinned official harness against a fixture or one explicitly selected instance when
the required Docker images are present. They are opt-in and labeled separately from the normal test suite.

The nested project has its own `pyproject.toml`, Hatchling configuration, package name, virtual environment, and
`uv.lock`. It does not depend on the root `powercontext` distribution, the Docker Python SDK, the Hugging Face
`datasets` library, or an installable official-harness package. Git, Docker, Codex, and the pinned harness are
invoked across subprocess boundaries.

The root repository gains `eval-lock-check`, `eval-test`, and `eval-check` Make targets plus an independent CI job.
Ordinary PowerContext runtime tests must not require Docker, Codex authentication, the network, or the SWE-bench
dataset.

## 15. Initial execution protocol

The first live run follows this order:

1. Run `doctor` on `m0`.
2. Resolve and record all revisions.
3. Select one candidate smoke instance.
4. Audit the task repository/test instructions for obvious credential-exfiltration behavior.
5. Run and pass the Gold Patch gate.
6. Run OFF.
7. Destroy the OFF task container while retaining artifacts.
8. Run ON from a new clean environment.
9. Destroy the ON task container while retaining artifacts.
10. Run the official evaluator for both predictions.
11. Validate OFF/ON treatment evidence.
12. Generate the report.
13. Re-run report generation and compare bytes to prove determinism.

The initial candidate is
`instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9`. This is an engineering selection, not an
officially designated sample. It is used only if its Gold Patch passes in the pinned local-Docker evaluator.
Its pinned dataset `dockerhub_tag` resolves through the harness image rule to
`jefzda/sweap-images:flipt-io.flipt-flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9`; adapters must not
mistake `dockerhub_tag` for a repository name.

Later statistical evaluation should use a curated Gold-verified subset, randomized arm order, repeated trials, and
confidence intervals. That is a follow-on milestone, not part of this MVP.

## 16. Acceptance evidence

Completion is established by:

- repository tests and checks passing from a clean checkout;
- the nested evaluation project's unit and contract tests passing;
- `powercontext-eval doctor --json` succeeding on `m0`;
- a retained Gold Patch bundle whose raw official evaluator output reports resolved;
- retained OFF and ON bundles for the same task and immutable inputs;
- official evaluator outputs for both arms;
- valid treatment evidence for both arms;
- a deterministic report that links every claim to an artifact;
- an artifact secret scan with zero findings;
- exact reproduction commands documented next to the report.
