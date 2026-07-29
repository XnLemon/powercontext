# Evaluation console operations

This console is for m0 only. It is an internal, unauthenticated service bound to m0's internal address
`100.88.99.11`; the host firewall and private network are the authentication boundary. Do not expose port 8787 to
the public Internet. The units do not manage or depend on the machine's existing application, database, cache, or
local proxy services.

## Fixed batch contract

One report is one immutable `swebench-pro-public-v2` batch:

- exactly 731 public SWE-bench Pro tasks;
- one PowerContext revision resolved to one full commit SHA for the complete batch;
- one `gpt-5.6-sol` / `medium` Codex configuration;
- one OFF and one ON execution for every task;
- exactly one physical OFF/ON task pair running globally while all other children remain in the durable queue.

The pinned dataset SHA-256 is
`b5b2462bfbf5aeb2cb7ba7d215778a1768b85f9d7ad7f748546c7f80a0ad1510`. The catalog refuses to start if the
file hash, row count, row schema, task IDs, or source order differs.

The production 731-task batch is expensive and long-running. Do not start it during deployment or smoke testing.
Before a real run, record and show the user:

```text
expected wall time = sum of the 731 observed OFF/ON pair durations
expected model cost = estimated OFF input/output cost + estimated ON input/output cost
cost ceiling = operator-approved maximum before submission
```

If representative production measurements do not yet exist, state that the estimate is unknown; do not invent one.
A real batch requires explicit final approval after the expected wall time, model cost, and cost ceiling are visible.

## Build and test the checkout

From `/data/powercontext-eval/deploy/powercontext`:

```sh
/data/powercontext-eval/bin/uv sync --project evaluation --frozen
/data/powercontext-eval/bin/uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests -m "not live" -q
/data/powercontext-eval/bin/uv run --project evaluation ruff check evaluation
/data/powercontext-eval/bin/uv run --project evaluation ruff format --check evaluation
/data/powercontext-eval/bin/uv run --directory evaluation ty check src tests
cd evaluation/web
npm ci
npm test -- --run
npm run build
```

Use the repository's committed `evaluation/web/package-lock.json`. The lock was generated with npm 11; use npm 11
if another major version rejects or rewrites it. The built frontend is
`/data/powercontext-eval/deploy/powercontext/evaluation/web/dist`.

## Install configuration and units

Create the configuration without exposing its eventual contents in logs:

```sh
install -d -m 0700 /data/powercontext-eval/config
install -m 0600 evaluation/deploy/powercontext-eval.env.example \
  /data/powercontext-eval/config/evaluation-console.env
chmod 0600 /data/powercontext-eval/config/evaluation-console.env
${EDITOR:?set EDITOR} /data/powercontext-eval/config/evaluation-console.env
test "$(stat -c %a /data/powercontext-eval/config/evaluation-console.env)" = 600
```

The example has no credential values. The Mac credential source is operator-supplied and must never enter Git or
the environment file. From the operator's Mac, copy it only to the explicit staging path inside the protected
configuration directory:

```sh
scp /operator/supplied/path/auth.json m0:/data/powercontext-eval/config/auth.json.staged
```

Then, on m0, install it without printing its contents and remove the staged file:

```sh
chmod 0600 /data/powercontext-eval/config/auth.json.staged
install -d -o rongfeng.frf -g users -m 0700 /data/powercontext-eval/codex-home
sudo install -o rongfeng.frf -g users -m 0600 \
  /data/powercontext-eval/config/auth.json.staged /data/powercontext-eval/codex-home/auth.json
unlink /data/powercontext-eval/config/auth.json.staged
sudo -u rongfeng.frf test -r /data/powercontext-eval/codex-home/auth.json
stat -c '%U:%G %a' /data/powercontext-eval/codex-home/auth.json | grep -qx 'rongfeng.frf:users 600'
```

Never paste authentication contents into terminal output, tickets, or logs.

Verify units before installation:

```sh
systemd-analyze verify evaluation/deploy/powercontext-eval-web.service \
  evaluation/deploy/powercontext-eval-worker.service
sudo install -m 0644 evaluation/deploy/powercontext-eval-web.service /etc/systemd/system/
sudo install -m 0644 evaluation/deploy/powercontext-eval-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now powercontext-eval-web.service powercontext-eval-worker.service
```

The m0 host uses systemd 219, so the units use its legacy `ReadOnlyDirectories=` and `ReadWriteDirectories=`
spellings (the current spellings are `ReadOnlyPaths=` and `ReadWritePaths=`). The filesystem namespace root is
read-only except `/data/powercontext-eval` and the service's private temporary directory. `ProtectSystem=full`
provides a second system-directory restriction compatible with systemd 219. The worker retains normal local
networking for the loopback proxy and joins the `docker` group to connect to `/var/run/docker.sock`; the Docker
daemon remains external to the namespace and is not managed by the unit. The web process does not receive Docker
group access.

## Verify and operate

```sh
systemctl status powercontext-eval-web.service powercontext-eval-worker.service
journalctl -u powercontext-eval-web.service -u powercontext-eval-worker.service --since today
curl --fail --show-error http://100.88.99.11:8787/api/health
```

Submitting work adds it to the SQLite-backed queue. One worker atomically leases a queued task for
`POWERCONTEXT_EVAL_LEASE_SECONDS`; polling is controlled by `POWERCONTEXT_EVAL_POLL_SECONDS`. A service crash causes
systemd to restart it after five seconds. An interrupted lease becomes eligible again after expiry, so restart can
delay a task but does not require manual queue editing. The web and worker share only the SQLite database and can
restart independently.

Persistent state and artifacts live under `/data/powercontext-eval`:

- Queue database: `/data/powercontext-eval/web/tasks.sqlite3`
- Per-run artifacts: `/data/powercontext-eval/runs/`
- Cached harness and dataset: `/data/powercontext-eval/cache/`
- Checkout and frontend snapshot: `/data/powercontext-eval/deploy/powercontext/`

Batch membership, source order, and the resolved PowerContext commit are stored durably. Completed children are
never rerun automatically. After restart, queued children remain queued; an expired running lease becomes an
interrupted child, and the worker continues with later queued children. Aggregate reports are rebuilt from the
immutable retained child artifacts.

## Report semantics and retained context

The report publishes measurements, not an authored acceptance conclusion:

- OFF and ON resolution rates use all 731 selected tasks as the denominator;
- a missing or failed evaluator result is not mislabeled as ordinary unresolved work and is counted separately as
  an execution/evaluation failure;
- the four paired outcome categories include only children with official results for both arms;
- input, output, and total Token comparisons are each calculated only from paired children for which both OFF and ON
  contain that metric; the displayed measured-task denominator is therefore identical for both arms;
- unavailable elapsed time and patch byte counts are omitted from the product report.

Each successful child retains the complete observable, sanitized OFF and ON timeline. The timeline contains the
benchmark prompt, Codex JSONL events, official evaluation, and exact PowerContext injections in timestamp order.
Injection records retain query, scope/session/turn identifiers when present, returned hit fields, and the exact
injected text. Codex authentication material, proxy credentials, environment secrets, and secret-shaped fields are
rejected before API delivery.

## Schema migration and release backup

The batch release migrates the existing SQLite database in place and preserves legacy task rows. Before deploying
the new SHA, stop only the evaluation worker and create a SQLite-consistent backup:

```sh
sudo systemctl stop powercontext-eval-worker.service
install -d -m 0700 /data/powercontext-eval/backups
sqlite3 /data/powercontext-eval/web/tasks.sqlite3 \
  ".backup '/data/powercontext-eval/backups/tasks-before-batch-<timestamp>.sqlite3'"
test -s /data/powercontext-eval/backups/tasks-before-batch-<timestamp>.sqlite3
```

Record the exact prior checkout SHA, unit files, environment-file checksum, Web/Worker status, and restart counts.
Deploy only the reviewed detached SHA, then initialize the schema by starting the Web process before the worker.
Do not delete the backup after successful startup.

## Preflight and acceptance

Run acceptance on m0 only. Before and after starting the console, record existing-service health with the site's
normal read-only health checks and compare results; the console units must not restart or reconfigure them. Also
check that port 8787 is free before first start:

```sh
ss -ltn 'sport = :8787'
curl --fail --show-error http://100.88.99.11:8787/api/health
```

For a secret scan that does not print matching values, inspect only filenames and exit status:

```sh
git grep -IlE '(api[_-]?key|password|token|secret)[[:space:]]*=' -- evaluation/deploy evaluation/README.md
test ! -f evaluation/deploy/evaluation-console.env
```

Docker cleanup audit: compare `docker ps -a --format '{{.ID}} {{.Names}} {{.Status}}'` and
`docker network ls --format '{{.ID}} {{.Name}}'` before and after an evaluation. Do not use broad prune commands;
investigate and remove only resources proven to belong to a completed run.

Verify the production catalog without starting work:

```sh
/data/powercontext-eval/bin/uv run --project evaluation python -c \
  "from pathlib import Path; from powercontext_eval.benchmarks.swebench_pro.catalog import SweBenchProCatalog; x=SweBenchProCatalog.load(Path('/data/powercontext-eval/cache/swebench-pro.git/helper_code/sweap_eval_full_v2.jsonl')); print(len(x.instance_ids), x.dataset_sha256)"
```

The output must be exactly the count `731` followed by the pinned SHA-256 above.

To validate batch creation and cancellation without accidentally launching Codex:

1. keep `powercontext-eval-worker.service` stopped;
2. create one batch through `POST /api/batches`;
3. verify it contains exactly 731 queued children and zero running children;
4. cancel it through `POST /api/batches/{batch_id}/cancel`;
5. verify all 731 children are cancelled;
6. restart the worker only after this check and only when no real batch is queued.

This create/cancel transaction is a deployment check, not authorization to run the paid benchmark.

## Rollback

Stop the two console units, check out the previously accepted commit in
`/data/powercontext-eval/deploy/powercontext`, rebuild the frontend and resync the frozen evaluation environment,
then start the units and repeat the m0 health and queue checks:

```sh
sudo systemctl stop powercontext-eval-worker.service powercontext-eval-web.service
git checkout --detach <prior-accepted-commit>
/data/powercontext-eval/bin/uv sync --project evaluation --frozen
(cd evaluation/web && npm ci && npm run build)
sudo systemctl start powercontext-eval-web.service powercontext-eval-worker.service
curl --fail --show-error http://100.88.99.11:8787/api/health
```

Rollback does not delete the queue or run artifacts. Back up the SQLite database before any schema-changing release.
If the batch migration itself must be rolled back, keep both services stopped, preserve the failed database for
forensics, restore the explicit pre-release SQLite backup, then start the prior Web SHA before the prior worker SHA.
