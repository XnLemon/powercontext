# TokensFlow Host and Evaluation-Container Telemetry Design

**Date:** 2026-08-02

**Status:** Approved design; implementation authorized

**Builds on:** `2026-08-01-task-pair-parallelism-design.md`

## 1. Purpose

The m0 evaluation host has a logged-in TokensFlow 1.0.16 installation, but its collector daemon is not running. The
SWE-bench Pro OFF/ON task containers run Codex in isolated homes and currently do not contain TokensFlow. As a result,
container Codex sessions are not collected by TokensFlow and a TokensFlow identity change on the host cannot be
verified inside an evaluation arm.

This change starts the m0 host collector and adds a TokensFlow collector to every OFF and ON task container. Every new
arm snapshots the currently selected Codex and TokensFlow configuration. Before Codex inference begins, the arm must
prove that `tokensflow whoami` returns the same identity content as the configured host invocation.

The implementation must:

- run the TokensFlow daemon persistently on m0 as the existing `rongfeng.frf` user;
- make the configured TokensFlow binary available inside every OFF and ON container;
- give every arm an isolated writable TokensFlow home seeded from the configured host TokensFlow home;
- start one TokensFlow daemon inside the same task container as Codex and PowerContext;
- compare host and container `tokensflow whoami` output before Codex inference;
- use deployment configuration for TokensFlow and Codex source paths rather than embedding account-specific paths or
  configuration contents in code;
- let a newly claimed arm observe configuration switches while keeping already running arms on their initial
  snapshots;
- keep credentials and identity text out of retained reports, events, errors, and service-health payloads;
- drain and verify TokensFlow collection before an arm can complete successfully, with a 60-second upper bound.

## 2. Scope and Non-Goals

### 2.1 In scope

- m0 host TokensFlow service activation and health verification;
- configurable TokensFlow binary and user-home source paths in the evaluation Web/Worker configuration;
- use of the existing configurable Codex binary and authentication source without introducing a second hard-coded
  Codex account path;
- private per-arm snapshots of `.tokensflow` configuration and credentials;
- a read-only TokensFlow binary mount and writable per-arm TokensFlow data home;
- exact identity-content comparison with only terminal line-ending normalization;
- container daemon startup before Codex execution;
- non-secret provenance containing versions, hashes, readiness state, and collection-process state;
- OFF/ON, task, and concurrent-task isolation;
- controlled m0 smoke validation before the current 731-task batch resumes.

### 2.2 Non-goals

- preventing duplicate uploads or deduplicating records in the evaluation runner;
- sharing a writable TokensFlow home between host, tasks, or OFF/ON arms;
- changing TokensFlow server behavior, ingest schemas, authentication format, update mechanism, or daemon polling;
- changing Codex models, reasoning effort, account-selection policy, quota accounting, or report formulas;
- restarting or reconfiguring Docker, new-api, MySQL, or Redis;
- retaining raw TokensFlow credentials, `whoami` output, daemon output, or upload payloads as evaluation artifacts.

## 3. Configuration Contract

The evaluation deployment adds two explicit source settings:

- `POWERCONTEXT_EVAL_TOKENSFLOW_BINARY`: absolute host path to the selected TokensFlow executable;
- `POWERCONTEXT_EVAL_TOKENSFLOW_USER_HOME`: absolute host user home whose `.tokensflow` directory is the selected
  TokensFlow configuration.

The existing settings remain authoritative for Codex:

- `POWERCONTEXT_EVAL_CODEX_BINARY` selects the Codex executable;
- `POWERCONTEXT_EVAL_AUTH_JSON` selects the current Codex authentication file.

No account identity, credential value, server URL, user-specific path, or configuration document is embedded in
Python defaults, Docker commands, reports, or tests. Package-local test defaults may be derived from the configured
evaluation root, while m0 supplies all production paths through its protected environment file.

Configuration is resolved by the Worker when it constructs a new attempt. The arm copies the selected source files
immediately before its container starts. Replacing the configured files atomically changes subsequent arms. A running
arm never observes later host changes, so an account switch cannot mix two identities within one arm.

Changing an environment-variable path still requires restarting only the evaluation Worker. Replacing content at the
already configured path requires no Worker restart. Neither operation resumes a paused batch automatically.

## 4. Host Daemon

TokensFlow already installed a user service at `~/.config/systemd/user/tokensflow.service`, but m0 currently has no
user manager runtime and no daemon process. Deployment enables lingering for the service account, starts its user
manager, and enables the existing TokensFlow-managed unit. It does not duplicate credentials into a system service or
construct a second account-specific unit.

Host acceptance requires all of the following:

1. the service is enabled and active under the `rongfeng.frf` user manager;
2. exactly one persistent `tokensflow daemon` process belongs to that service;
3. `tokensflow status` can observe the daemon;
4. configured-host `tokensflow whoami` exits successfully;
5. new-api, MySQL, Redis, Web, Worker, and Docker remain unchanged and healthy.

If the user manager cannot be activated safely, deployment stops and retains diagnostics. It does not fall back to an
unmanaged `nohup` process or a separately hard-coded root service.

## 5. Per-Arm Runtime Layout

Every OFF and ON arm already owns a fresh private runtime directory. It gains:

```text
runtime/
  tokensflow-home/
    .tokensflow/       # private snapshot of selected host configuration
    .local/share/tokensflow/
  tokensflow/
    provenance.json    # non-secret hashes, version, and readiness flags
```

The `.tokensflow` source must be a real directory within the configured user home. Snapshotting rejects symlinked
roots, non-regular credential/configuration files, unsafe permissions, special files, and destinations that already
exist. The private copy is owned by the container UID and is never retained under report artifacts.

The selected TokensFlow executable is validated as a regular executable and mounted read-only using the same
directory-mount boundary used for Codex and uv. The container receives its own writable `HOME` pointing at
`/runtime/tokensflow-home`; Codex continues using the arm-specific `CODEX_HOME=/runtime/codex-home`.

## 6. Identity Gate and Daemon Lifecycle

After the task container and PowerContext server start, but before Codex inference:

1. execute the configured host TokensFlow binary with `HOME` set to the configured TokensFlow user home;
2. execute the mounted container TokensFlow binary with `HOME=/runtime/tokensflow-home`;
3. require both `whoami` commands to exit successfully within a bounded timeout;
4. normalize only a terminal `LF` versus `CRLF` and the final newline;
5. compare the remaining output bytes exactly;
6. retain only SHA-256 hashes, byte lengths, binary versions, and a boolean match result;
7. reject mismatches as a sanitized infrastructure failure before Codex starts;
8. launch `tokensflow daemon` as a detached process inside the same task container, with the arm's `HOME`,
   `CODEX_HOME`, network proxy, and isolated data directory;
9. verify that the daemon process remains alive after a short bounded readiness interval, then start Codex.

After Codex finishes, the runner freezes the arm's transcript-producing work and gives TokensFlow at most 60 seconds
to complete this drain sequence:

1. signal the container daemon to terminate normally and wait for its process to exit;
2. run `tokensflow upload --all` in the same container and private home so every complete local JSONL line is
   submitted independently of daemon offsets;
3. require the upload command to exit successfully;
4. verify TokensFlow reports no pending or blocked ingest work;
5. write non-secret drain provenance and only then continue to treatment validation and container cleanup.

Duplicate records are acceptable because server-side ingest deduplicates replayed slices. Missing collection is not
acceptable. If the sequence exceeds 60 seconds or any drain check fails, the arm fails as infrastructure, the batch
requests pause, and the private TokensFlow home and Codex JSONL are preserved outside retained public reports for
recovery. The runner does not destroy the only recoverable copy of an undrained arm.

## 7. Security and Failure Semantics

TokensFlow credentials join the existing secret-redaction boundary. Secret variants are loaded from both the selected
Codex authentication file and the TokensFlow credential snapshot, then rejected from retained logs and artifacts.
Raw `whoami`, daemon stdout/stderr, configuration, status output, and upload responses are private runtime data and are
deleted with the arm.

The container keeps its existing read-only root filesystem, dropped capabilities, no-new-privileges setting, UID,
CPU/memory/PID limits, task network, proxy relay, and task-scoped mounts. TokensFlow receives no Docker socket, host
home, shared state directory, or additional network privilege.

The following fail before Codex inference and request the existing infrastructure-failure batch pause:

- missing or unsafe configured binary/home/configuration;
- host or container `whoami` timeout or non-zero exit;
- identity-content mismatch;
- failure to start the container daemon;
- daemon exit during the bounded readiness check.

Daemon exit after readiness, graceful-stop failure, final-upload failure, a blocked/pending queue after upload, or the
60-second drain timeout fails the arm as infrastructure. Codex output remains available for diagnosis, but official
evaluation and task success cannot proceed until collection is complete or the logical task is safely retried.

## 8. Observability

Task provenance adds non-secret TokensFlow fields:

- host and container binary version;
- host and container `whoami` output SHA-256 and byte length;
- `identity_match`;
- `daemon_started`, readiness timestamp, graceful-stop result, final-upload result, and drain duration;
- configured-path fingerprints that cannot reconstruct the paths themselves.

Health APIs do not expose identity, TokensFlow configuration, credentials, or raw daemon output. Operational
monitoring adds host-service active state and the count of TokensFlow daemon processes inside active evaluation
containers. With four running task pairs, there are at most four active arm containers and therefore at most four
container daemons, plus one host daemon.

## 9. Test and Acceptance Matrix

| Scenario | Expected observable behavior |
| --- | --- |
| Configured paths | Worker passes configured TokensFlow binary/home and existing Codex binary/auth paths into each run |
| Content switch | Atomic replacement at a configured path affects the next arm without changing a running arm snapshot |
| Path switch | Updating the protected environment and restarting only Worker selects the new paths without resuming a batch |
| Safe snapshot | Regular private configuration files copy to a fresh arm home with restrictive permissions |
| Unsafe source | Symlinked roots/files, special files, missing credentials, or unsafe destinations fail before container start |
| Binary mount | Container executes the configured binary from a read-only tool mount rather than the task image |
| Identity match | Equivalent host/container `whoami` content passes and only hashes/lengths are retained |
| Identity mismatch | Different content fails before Codex and records no raw identity text |
| Daemon startup | One detached daemon starts inside each active arm and survives the bounded readiness check |
| Graceful stop | Codex completion normally terminates and joins the container daemon before cleanup |
| Complete drain | `upload --all` succeeds and queue inspection shows no pending or blocked collection before success |
| Drain timeout | The combined stop/upload/verification sequence exceeding 60 seconds fails infrastructure and preserves spool |
| Drain failure | Upload or queue verification failure pauses the batch and preserves the only recoverable private data |
| OFF/ON isolation | OFF and ON use distinct TokenFlow homes, data directories, identity evidence, and daemon processes |
| Parallel isolation | Concurrent tasks never share writable TokenFlow state or exceed one daemon per active arm container |
| Secret boundary | Credentials, identity content, status output, and daemon/upload output never enter retained artifacts/errors |
| Host service | One user-managed m0 daemon is enabled, active, and visible to `tokensflow status` |
| Existing behavior | Gold, OFF/ON Codex, PowerContext injection, official evaluation, reports, usage pause, and cleanup remain valid |

Implementation follows RED/GREEN TDD. Validation runs focused configuration, runner, SUT, security, and deployment
tests; the full backend and frontend suites; Ruff; formatting; full ty; and the frontend build locally and on m0.

## 10. m0 Deployment and Controlled Rollout

The current batch is requested to pause, and already claimed task pairs reach their normal task boundary. No new task
is claimed during deployment. The implementation is delivered as Linux source and validated on m0; no Mac-built
artifact is deployed.

Deployment then:

1. confirms Codex usage is below 80 percent, the batch is fully paused, and dependent services are healthy;
2. enables and verifies the existing TokensFlow user service on m0;
3. deploys the Worker configuration and code, restarting only the evaluation Worker;
4. runs a disposable task-image smoke proving binary execution, configuration snapshot, exact `whoami` match, daemon
   readiness, graceful stop, complete upload, queue drain, network access, and cleanup without exposing identity text;
5. resumes one controlled logical task, immediately requests pause after it is claimed, and lets OFF and ON complete;
6. verifies both arms' TokensFlow provenance, daemon presence during inference, drain completion within 60 seconds,
   Codex/PowerContext behavior, official evaluation, report generation, and container/network/private-runtime cleanup;
7. confirms the batch is paused with zero running attempts and all services remain healthy;
8. restores the configured task-pair parallelism and explicitly resumes sustained execution only when usage remains
   below 80 percent and no infrastructure failure remains.

If validation fails, the batch remains paused, evidence is retained, and only the failed logical task is retried after
an evidence-based TDD repair. Deployment never restarts or reconfigures Docker and never restarts new-api, MySQL, or
Redis.
