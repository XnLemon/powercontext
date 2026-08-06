# Source559 Official Evaluator Proxy Isolation Design

## Context

Source559 uses a verified reference Gold patch because the public dataset Gold patch is defective. Attempts 4 and 5
still failed in `TestProxySSHDial` at `proxy_test.go:88`, while two direct invocations of the pinned official evaluator
passed all 53 required tests. A controlled m0 A/B run held the instance row, prediction SHA, image, harness commit,
Python interpreter, working directory, and CLI flags constant:

- without the evaluator Docker proxy configuration: 53/53 passed;
- with the production `OfficialEvaluator` proxy configuration: `TestProxySSHDial` failed with the same nil-pointer
  stack as attempts 4 and 5.

The production path writes a temporary Docker client configuration. Docker SDK then injects its HTTP proxy variables
into the task container. That infrastructure detail changes Teleport's proxy test semantics.

## Decision

Only source559 will run its official evaluator without the Docker client proxy configuration. The existing exact-instance
Gold selection is the fail-closed authority for this choice. The same evaluator instance is reused for Gold, OFF, and ON,
so all three official checks use one consistent transport policy.

All other tasks continue using the configured Docker proxy. Image resolution remains unchanged and can still use the
registry proxy before official evaluation. The pinned harness, dataset row, Gold reference patch, OFF/ON model patches,
Docker daemon configuration, and aggregate result semantics remain unchanged.

## Audit model

`GoldValidationSelection` and `GoldValidationAudit` gain an `official_evaluation_transport` field with two allowed values:

- `docker_proxy`: the existing default for ordinary tasks;
- `proxy_bypassed_for_test_isolation`: required for the exact source559 verified override.

Old ordinary reports remain readable through the default. Source559 reports fail closed unless the verified reference
provenance, successful Gold status, and proxy-bypass transport are all present. The rendered report displays the
transport value.

## Execution flow

1. Select and validate the Gold patch before constructing `OfficialEvaluator`.
2. Use `ProxyRelayConfig(config.proxy_url)` for `docker_proxy`.
3. Use `proxy=None` for `proxy_bypassed_for_test_isolation`.
4. Reuse that evaluator for Gold, OFF, and ON.
5. Persist the transport in `gold/validation.json` and the final report.

## Failure handling

Unknown transport values are rejected by strict model validation. Source559 hash or provenance mismatches continue to
fail before an evaluator starts. No fallback from proxy bypass to proxy is allowed for source559, because that would
reintroduce the confirmed semantic contamination.

## Verification

- RED/GREEN unit test: source559 constructs one evaluator with no proxy and invokes it three times.
- Regression test: an ordinary task still constructs one proxy-configured evaluator and invokes it three times.
- Audit tests: source559 requires the bypass transport; ordinary legacy audit data defaults to `docker_proxy`.
- Report test: rendered Gold validation includes the transport.
- Full backend, Ruff, formatting, source type checks, and frontend tests locally and on m0.
- Controlled source559 attempt6 at parallelism 1 while the batch remains pause-controlled.

## Operational recovery

After attempt6 passes Gold, OFF, ON, official evaluation, audit, and report validation, restore parallelism 20, restart
only the idle evaluation Worker, verify the batch is still paused and healthy, then explicitly resume the main queue.

