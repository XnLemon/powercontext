# Evaluation console operations

This console is for m0 only. It is an internal, unauthenticated service bound to m0's internal address
`100.88.99.11`; the host firewall and private network are the authentication boundary. Do not expose port 8787 to
the public Internet. The units do not manage or depend on the machine's existing application, database, cache, or
local proxy services.

## Build and test the checkout

From `/data/powercontext-eval/deploy/powercontext`:

```sh
/data/powercontext-eval/bin/uv sync --project evaluation --frozen
/data/powercontext-eval/bin/uv run --project evaluation pytest
/data/powercontext-eval/bin/uv run --project evaluation ruff check evaluation
/data/powercontext-eval/bin/uv run --project evaluation ruff format --check evaluation
/data/powercontext-eval/bin/uv run --project evaluation ty check --project evaluation
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
install -m 0600 evaluation/deploy/evaluation-console.env.example \
  /data/powercontext-eval/config/evaluation-console.env
chmod 0600 /data/powercontext-eval/config/evaluation-console.env
${EDITOR:?set EDITOR} /data/powercontext-eval/config/evaluation-console.env
test "$(stat -c %a /data/powercontext-eval/config/evaluation-console.env)" = 600
```

The example has no credential values. Authentication is copied separately to
`/data/powercontext-eval/codex-home/auth.json` with mode 0600. Never paste its contents into the environment file,
terminal output, tickets, or logs.

Verify units before installation:

```sh
systemd-analyze verify evaluation/deploy/powercontext-eval-web.service \
  evaluation/deploy/powercontext-eval-worker.service
sudo install -m 0644 evaluation/deploy/powercontext-eval-web.service /etc/systemd/system/
sudo install -m 0644 evaluation/deploy/powercontext-eval-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now powercontext-eval-web.service powercontext-eval-worker.service
```

The m0 host uses systemd 219, so the units use its legacy `ReadWriteDirectories=` spelling (the current spelling is
`ReadWritePaths=`). It grants writes only under `/data/powercontext-eval`. `ProtectSystem=full` is the strongest
compatible systemd-219 mode. The worker retains normal local networking for the loopback proxy and joins the
`docker` group to reach `/var/run/docker.sock`; the web process does not receive Docker group access.

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
