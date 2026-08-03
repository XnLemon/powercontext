# Evaluation Service Shutdown Design

## Goal

Make Worker and Web shutdown deterministic under systemd without losing durable TokensFlow finalization work or
treating forced termination as success.

## Worker signal boundary

The Worker signal context owns a closure-local boolean. The first SIGTERM or SIGINT sets it before calling
`worker.stop()`. A repeated signal returns immediately and does not enter any Event or Lock. No synchronization
primitive is added to the Python signal handler. The first signal must still invoke `worker.stop()` exactly once.

Both service units use `KillMode=mixed`. systemd sends SIGTERM only to the `uv` MainPID, and `uv` forwards it once to
the Python child. If graceful shutdown exceeds the service deadline, systemd may still send SIGKILL to the complete
control group.

## Cooperative finalizer cancellation

The Worker stop Event flows through `TokensFlowFinalizer.run_once`, each claimed job, the Docker runtime, and
`ProcessRunner`. ProcessRunner checks cancellation while waiting for the child it created. On cancellation it
terminates only that child's verified independent process group and raises a sanitized cancellation error.

The job boundary catches cancellation and promptly releases the owned durable lease to `pending`, with no owner or
lease expiry. A current or replacement finalizer can therefore claim it immediately. Executor workers remain
non-daemon; prompt shutdown comes from ending their active child processes and joining completed threads, not from
abandoning them. No command output, environment value, path, credential, or token is persisted in the cancellation
state.

## Web exit status

The Web service declares only exit status 143 as an additional successful status because uvicorn deliberately
re-raises handled SIGTERM and `uv` converts it to 143. SIGKILL status 137 remains a failure.

## Tests

- A subprocess using the real Worker signal context receives a second SIGTERM while its first `stop()` call holds a
  normal Lock. It must exit within one second and record exactly one stop call.
- Separate upload- and doctor-blocked tests stop the finalizer, require bounded supervisor exit, verify the job is
  `pending` without a lease owner, and prove immediate reclaim.
- Deployment tests require `KillMode=mixed` on both units and `SuccessExitStatus=143` only on Web.

No m0 or live operation is part of this change.
