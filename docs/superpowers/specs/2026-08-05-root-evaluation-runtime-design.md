# Root evaluation runtime

## Goal

Run SWE-bench Pro tasks with the task image's native root-user behavior so evaluation infrastructure does not
create permission failures that the benchmark task itself would not have.

## Runtime contract

- The host evaluation Worker service runs as root. The Web service remains unchanged because it does not execute
  benchmark workloads.
- Task, setup, fallback-copy, and official-evaluator containers do not override the image user.
- Evaluation containers use a writable disposable root filesystem and retain Docker's normal container
  capabilities and privilege behavior.
- Codex and TokensFlow use root's default home directories: `/root/.codex` and `/root/.tokensflow`. The runner does
  not set `HOME` or `CODEX_HOME`.
- Credentials and configuration are staged into the per-arm container home without hard-coded account contents.
- Every OFF and ON arm remains a separate disposable container with scoped mounts and network, no Docker socket,
  CPU/memory/PID limits, and normal cleanup after successful execution.
- Infrastructure failures preserve the container, network, workspace, runtime, and logs for diagnosis. Preserved
  resources are deleted only by an explicit operator cleanup after evidence has been collected.
- The Worker claims no new tasks while the runtime change is deployed and proven by one real task.

## Compatibility and cleanup

The runner copies required results out before deleting the task container. Host-side retained artifacts remain
owned and managed by the root Worker, so no UID translation or recursive ownership repair is required. Existing
report schemas, OFF/ON treatment validation, TokensFlow finalization, proxy routing, and official grading remain
unchanged.

## Verification

Contract tests must reject the old `--user 2950:100`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, `HOME`,
and `CODEX_HOME` arguments on executable evaluation containers. Local and m0 full regression suites must pass. A
paused-batch retry must then prove `/root/.codex`, `/root/.tokensflow`, Codex execution, TokensFlow finalization,
official grading, reporting, and cleanup before normal Luna execution resumes.
