# PowerContext Evaluation Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a desktop-first internal console that durably queues PowerContext evaluations, runs one task at a time on m0, and renders the system-generated acceptance report.

**Architecture:** A same-origin FastAPI application serves a React/Vite frontend and a validated JSON API. A dedicated worker claims FIFO tasks from SQLite under a renewable single-worker lease, invokes the existing `run_minimal_swebench_pro` runner, and indexes the retained report artifacts without duplicating them.

**Tech Stack:** Python 3.11, FastAPI, Uvicorn, Pydantic 2, SQLite, React 19, TypeScript, Vite, Vitest, Testing Library, Playwright, systemd

---

## File Map

### Existing files to modify

- `evaluation/pyproject.toml` — add Web runtime and test dependencies plus Web/worker commands.
- `evaluation/uv.lock` — lock Python dependency changes.
- `evaluation/src/powercontext_eval/runner.py` — emit truthful phase callbacks and persist validated `report.json`.
- `evaluation/src/powercontext_eval/powercontext_sut.py` — expose OFF/ON arm start callbacks without changing execution order.
- `evaluation/src/powercontext_eval/cli.py` — add `web` and `worker` commands.
- `evaluation/tests/unit/test_report.py` — verify JSON report round-trip remains safe.
- `evaluation/tests/contract/test_codex_contract.py` — verify OFF/ON phase callbacks preserve execution behavior.

### Backend files to create

- `evaluation/src/powercontext_eval/web/__init__.py` — public Web package exports.
- `evaluation/src/powercontext_eval/web/config.py` — immutable environment-derived service configuration.
- `evaluation/src/powercontext_eval/web/models.py` — API and lifecycle models.
- `evaluation/src/powercontext_eval/web/store.py` — SQLite schema, task repository, FIFO claim, lease, and recovery.
- `evaluation/src/powercontext_eval/web/reporting.py` — validated artifact-to-API report projection.
- `evaluation/src/powercontext_eval/web/worker.py` — one-task worker orchestration and safe failure mapping.
- `evaluation/src/powercontext_eval/web/api.py` — FastAPI routes, server-sent events, and static application serving.

### Backend tests to create

- `evaluation/tests/web/test_config.py`
- `evaluation/tests/web/test_store.py`
- `evaluation/tests/web/test_reporting.py`
- `evaluation/tests/web/test_worker.py`
- `evaluation/tests/web/test_api.py`
- `evaluation/tests/web/test_cli.py`

### Frontend files to create

- `evaluation/web/package.json`
- `evaluation/web/package-lock.json`
- `evaluation/web/tsconfig.json`
- `evaluation/web/vite.config.ts`
- `evaluation/web/index.html`
- `evaluation/web/src/main.tsx`
- `evaluation/web/src/types.ts`
- `evaluation/web/src/api.ts`
- `evaluation/web/src/App.tsx`
- `evaluation/web/src/components/AppShell.tsx`
- `evaluation/web/src/components/TaskForm.tsx`
- `evaluation/web/src/components/TaskList.tsx`
- `evaluation/web/src/components/TaskDetail.tsx`
- `evaluation/web/src/components/ReportView.tsx`
- `evaluation/web/src/styles.css`
- `evaluation/web/src/test/setup.ts`
- `evaluation/web/src/**/*.test.tsx`
- `evaluation/web/e2e/console.spec.ts`
- `evaluation/web/playwright.config.ts`

### Deployment files to create

- `evaluation/deploy/powercontext-eval-web.service`
- `evaluation/deploy/powercontext-eval-worker.service`
- `evaluation/deploy/powercontext-eval.env.example`
- `evaluation/README.md`

## Task 1: Add truthful runner phases and a machine-readable report

**Files:**
- Modify: `evaluation/src/powercontext_eval/runner.py`
- Modify: `evaluation/src/powercontext_eval/powercontext_sut.py`
- Modify: `evaluation/tests/unit/test_report.py`
- Modify: `evaluation/tests/contract/test_codex_contract.py`
- Create: `evaluation/tests/unit/test_runner_phases.py`

- [ ] **Step 1: Write failing tests for ordered phase callbacks**

Add a callback contract to a focused runner test:

```python
def test_run_phases_are_stable_and_truthful() -> None:
    assert list(RunPhase) == [
        RunPhase.PREPARING,
        RunPhase.VALIDATING_GOLD,
        RunPhase.RUNNING_OFF,
        RunPhase.RUNNING_ON,
        RunPhase.OFFICIAL_EVALUATION,
        RunPhase.GENERATING_REPORT,
    ]
```

Extend the existing `DockerSut.run_pair` contract fixture so it records `before_arm` calls and asserts:

```python
assert arm_starts == [Arm.OFF, Arm.ON]
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/unit/test_runner_phases.py \
  evaluation/tests/contract/test_codex_contract.py -q
```

Expected: collection or import failure because `RunPhase` and `before_arm` do not exist.

- [ ] **Step 3: Add phase types and callbacks**

Add:

```python
class RunPhase(StrEnum):
    PREPARING = "preparing"
    VALIDATING_GOLD = "validating_gold"
    RUNNING_OFF = "running_off"
    RUNNING_ON = "running_on"
    OFFICIAL_EVALUATION = "official_evaluation"
    GENERATING_REPORT = "generating_report"
```

Change the runner signature to:

```python
PhaseCallback = Callable[[RunPhase], None]

def run_minimal_swebench_pro(
    config: MinimalRunConfig,
    *,
    on_phase: PhaseCallback | None = None,
) -> MinimalRunResult:
```

Use a no-op callback when none is supplied. Call it immediately before the real boundary it names. Extend
`DockerSut.run_pair(..., before_arm: Callable[[Arm], None] | None = None)` and invoke the callback directly before
each `_execute_arm` call. Do not emit percentages.

- [ ] **Step 4: Write a failing test for validated JSON retention**

Extend the report and runner tests to assert:

```python
payload = json.loads((run_root / "report.json").read_text())
assert ReportBundle.model_validate(payload, strict=True) == expected_bundle
assert "authorization" not in (run_root / "report.json").read_text().casefold()
```

- [ ] **Step 5: Verify the JSON test fails**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/unit/test_report.py evaluation/tests/unit/test_runner_phases.py -q
```

Expected: failure because `report.json` is absent.

- [ ] **Step 6: Persist the already validated report bundle**

After deterministic Markdown validation, write:

```python
run_store.create_json("report.json", report.model_dump(mode="json"))
```

Keep `report.md` unchanged and return its existing path.

- [ ] **Step 7: Run runner and contract tests**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/unit/test_report.py \
  evaluation/tests/unit/test_runner_phases.py \
  evaluation/tests/contract/test_codex_contract.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add evaluation/src/powercontext_eval/runner.py \
  evaluation/src/powercontext_eval/powercontext_sut.py \
  evaluation/tests/unit/test_report.py \
  evaluation/tests/unit/test_runner_phases.py \
  evaluation/tests/contract/test_codex_contract.py
git commit -m "feat(eval): expose run phases and report data"
```

## Task 2: Add immutable Web configuration and task models

**Files:**
- Modify: `evaluation/pyproject.toml`
- Modify: `evaluation/uv.lock`
- Create: `evaluation/src/powercontext_eval/web/__init__.py`
- Create: `evaluation/src/powercontext_eval/web/config.py`
- Create: `evaluation/src/powercontext_eval/web/models.py`
- Create: `evaluation/tests/web/test_config.py`

- [ ] **Step 1: Write failing configuration and validation tests**

Test an explicit temporary root and capability allowlists:

```python
def test_web_config_derives_confined_paths(tmp_path: Path) -> None:
    config = WebConfig.for_root(tmp_path)
    assert config.database_path == tmp_path / "web" / "tasks.sqlite3"
    assert config.run_root == tmp_path
    assert config.frontend_dist.name == "dist"


def test_task_request_rejects_arbitrary_revision() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            powercontext_ref="main; shutdown",
            benchmark="swebench-pro",
            instance_id=INSTANCE_ID,
            model="gpt-5.6-sol",
            reasoning_effort="medium",
            treatment_mode="off_on",
        )
```

Also cover a full 40-character commit, `latest`, unknown benchmark/model/instance/reasoning/treatment values, and an
invalid idempotency key.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_config.py -q
```

Expected: import failure because the Web package does not exist.

- [ ] **Step 3: Add dependencies and models**

Add runtime dependencies:

```toml
"fastapi>=0.116,<1",
"uvicorn>=0.35,<1",
```

Add development dependencies:

```toml
"httpx>=0.28,<1",
```

Define strict Pydantic models and string enums:

```python
class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    powercontext_ref: str
    benchmark: Literal["swebench-pro"]
    instance_id: Literal[INSTANCE_ID]
    model: Literal["gpt-5.6-sol"]
    reasoning_effort: Literal["medium"]
    treatment_mode: Literal["off_on"]
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")

    @field_validator("powercontext_ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        PowerContextRef.parse(value)
        if value != "latest" and not value.startswith("commit:"):
            raise ValueError("Web evaluations accept only latest or an exact commit")
        return value
```

Define `TaskRecord`, `TaskSummary`, `TaskEvent`, `Capabilities`, `HealthResponse`, and `ReportResponse` with strict
fields matching the design.

- [ ] **Step 4: Add immutable configuration**

`WebConfig.for_root(root)` must derive all defaults from one root. `WebConfig.from_environment()` reads only named
`POWERCONTEXT_EVAL_*` variables, validates absolute paths and numeric lease durations, and never exposes proxy or
credential values through its public serialization.

- [ ] **Step 5: Lock and run tests**

Run:

```bash
uv lock --project evaluation
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_config.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add evaluation/pyproject.toml evaluation/uv.lock \
  evaluation/src/powercontext_eval/web \
  evaluation/tests/web/test_config.py
git commit -m "feat(eval): define evaluation console domain"
```

## Task 3: Implement the durable FIFO task store

**Files:**
- Create: `evaluation/src/powercontext_eval/web/store.py`
- Create: `evaluation/tests/web/test_store.py`

- [ ] **Step 1: Write failing store tests**

Cover schema creation, idempotent create, FIFO ordering, cancellation, claim exclusion, heartbeat, completion,
failure, and expired lease recovery. Use a real temporary SQLite database.

Core concurrency assertion:

```python
first = store.claim_next("worker-a", now=clock.now)
second = store.claim_next("worker-b", now=clock.now)
assert first is not None
assert second is None
assert store.list_tasks(status=TaskStatus.QUEUED)[0].task_id == queued_second.task_id
```

Recovery assertion:

```python
store.recover_expired(now=clock.now + timedelta(seconds=61))
assert store.get(first.task_id).status is TaskStatus.INTERRUPTED
assert store.get(first.task_id).failure_category == "worker_interruption"
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_store.py -q
```

Expected: import failure because `TaskStore` does not exist.

- [ ] **Step 3: Create the SQLite schema**

Use a `tasks` table with immutable request JSON plus indexed lifecycle columns, and a singleton `worker_lease`
table. Enable:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

Use `BEGIN IMMEDIATE` for claim, cancellation, and lease updates. Store UTC timestamps as RFC 3339 strings.

- [ ] **Step 4: Implement repository operations**

Provide:

```python
class TaskStore:
    def initialize(self) -> None: ...
    def create(self, request: TaskCreate, *, now: datetime) -> tuple[TaskRecord, bool]: ...
    def get(self, task_id: str) -> TaskRecord: ...
    def list_tasks(self, *, status: TaskStatus | None, limit: int, offset: int) -> list[TaskSummary]: ...
    def cancel_queued(self, task_id: str, *, now: datetime) -> TaskRecord: ...
    def claim_next(self, worker_id: str, *, now: datetime) -> TaskRecord | None: ...
    def heartbeat(self, task_id: str, worker_id: str, *, now: datetime) -> None: ...
    def set_phase(self, task_id: str, worker_id: str, phase: TaskPhase, *, now: datetime) -> None: ...
    def succeed(self, task_id: str, worker_id: str, result: TaskResult, *, now: datetime) -> TaskRecord: ...
    def fail(self, task_id: str, worker_id: str, failure: SafeFailure, *, now: datetime) -> TaskRecord: ...
    def recover_expired(self, *, now: datetime) -> list[str]: ...
```

Every state change increments an integer `version` used by server-sent events. Terminal tasks are immutable.

- [ ] **Step 5: Run store tests**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_store.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add evaluation/src/powercontext_eval/web/store.py evaluation/tests/web/test_store.py
git commit -m "feat(eval): add durable task queue"
```

## Task 4: Project validated report artifacts into the API

**Files:**
- Create: `evaluation/src/powercontext_eval/web/reporting.py`
- Create: `evaluation/tests/web/test_reporting.py`

- [ ] **Step 1: Write failing report projection tests**

Build a real `ReportBundle`, persist it as `report.json`, and assert:

```python
response = load_report(run_dir)
assert response.acceptance_valid is True
assert response.off.resolution == "resolved"
assert response.on.resolution == "resolved"
assert response.comparison.input_tokens.delta == -841_014
assert response.comparison.input_tokens.percent == pytest.approx(-42.839)
```

Also assert rejection of:

- missing or malformed `report.json`;
- a run directory outside the configured root;
- wrong OFF/ON arm roles;
- invalid treatment evidence;
- Markdown containing `<script>` is returned as plain text and never trusted HTML.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_reporting.py -q
```

Expected: import failure because `load_report` does not exist.

- [ ] **Step 3: Implement validated projection**

Load bytes with a bounded maximum size, validate through `ReportBundle.model_validate_json(..., strict=True)`, and
derive comparison values only when both treatments are valid and metrics are present.

Read `arms/{off,on}/powercontext/treatment.json` through a strict model containing exactly:

```python
class TreatmentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mcp_requests: int = Field(ge=0)
    prompt_sources: int = Field(ge=0)
    plugin_checkout_sha: str
    plugin_id: str
    plugin_installed: bool
    plugin_version: str
    scope_id: str
    server_ready: bool
```

Verify OFF has zero activity and ON has positive prompt-source and MCP activity before setting
`acceptance_valid=True`.

- [ ] **Step 4: Run report tests**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_reporting.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/powercontext_eval/web/reporting.py evaluation/tests/web/test_reporting.py
git commit -m "feat(eval): expose validated acceptance reports"
```

## Task 5: Implement the serial evaluation worker

**Files:**
- Create: `evaluation/src/powercontext_eval/web/worker.py`
- Create: `evaluation/tests/web/test_worker.py`

- [ ] **Step 1: Write failing worker behavior tests**

Inject a fake runner function and deterministic clock. Assert:

```python
assert worker.run_once() is True
assert calls[0].run_id == task.task_id
assert store.get(task.task_id).status is TaskStatus.SUCCEEDED
```

Cover:

- no work returns `False`;
- only one worker can claim;
- callback phases update the store;
- runner exceptions become fixed safe categories;
- exception text containing a fake credential is absent from the stored failure;
- an existing task artifact directory causes a safe failure rather than overwrite;
- heartbeats keep a long task lease alive.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_worker.py -q
```

Expected: import failure because `EvaluationWorker` does not exist.

- [ ] **Step 3: Implement worker configuration mapping**

Map `TaskCreate` to `MinimalRunConfig` using only `WebConfig` paths and validated request fields. The task ID is the
runner Run ID.

Use:

```python
result = self._runner(config, on_phase=self._phase_callback(task.task_id))
```

Start a bounded heartbeat thread while the blocking runner executes. Always stop and join it in `finally`.

- [ ] **Step 4: Implement safe failure mapping**

Map known evaluation exception types to fixed `FailureCategory` values. For unknown exceptions store:

```python
SafeFailure(
    category=FailureCategory.INTERNAL,
    summary="The evaluation worker failed unexpectedly. Inspect the retained m0 logs.",
)
```

Never store `str(error)` for an unknown exception.

- [ ] **Step 5: Run worker tests**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_worker.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add evaluation/src/powercontext_eval/web/worker.py evaluation/tests/web/test_worker.py
git commit -m "feat(eval): run queued evaluations serially"
```

## Task 6: Add the FastAPI control plane and server-sent events

**Files:**
- Create: `evaluation/src/powercontext_eval/web/api.py`
- Create: `evaluation/tests/web/test_api.py`

- [ ] **Step 1: Write failing API contract tests**

Use `fastapi.testclient.TestClient` with a real temporary store. Cover:

```python
response = client.post("/api/tasks", json=valid_payload)
assert response.status_code == 201
assert response.json()["status"] == "queued"
assert response.json()["queue_position"] == 1
```

Also cover health, capabilities, duplicate idempotency, list pagination, task details, queued cancellation, invalid
request rejection, structured report, raw Markdown `text/plain`, missing report, and no secret value in any response.

For events, create a task, update its version, and assert the stream contains:

```text
event: task
data: {"task_id":...
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_api.py -q
```

Expected: import failure because `create_app` does not exist.

- [ ] **Step 3: Implement routes and error envelopes**

Create:

```python
def create_app(config: WebConfig, store: TaskStore | None = None) -> FastAPI:
    ...
```

Use sync route functions for SQLite work. Return a stable error body:

```json
{"error":{"code":"task_not_found","message":"The requested evaluation task does not exist."}}
```

Set `Cache-Control: no-store` on task, event, and report endpoints.

- [ ] **Step 4: Implement bounded SSE**

The generator polls task `version`, emits only on change, sends a heartbeat comment every 15 seconds, and exits
when the client disconnects or the task reaches a terminal state. It must not hold a SQLite transaction while
waiting.

- [ ] **Step 5: Serve the frontend safely**

When `frontend_dist` exists, mount hashed assets and return `index.html` for non-API routes. API 404s must remain
JSON and must never fall through to the application shell.

- [ ] **Step 6: Run API tests**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_api.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add evaluation/src/powercontext_eval/web/api.py evaluation/tests/web/test_api.py
git commit -m "feat(eval): add evaluation console API"
```

## Task 7: Add Web and worker command entry points

**Files:**
- Modify: `evaluation/pyproject.toml`
- Modify: `evaluation/src/powercontext_eval/cli.py`
- Create: `evaluation/tests/web/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Assert help exposes:

```text
powercontext-eval web
powercontext-eval worker
```

Inject `uvicorn.run` and a fake worker loop to verify configured host, port, root, and polling interval without
opening sockets or executing an evaluation.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/web/test_cli.py -q
```

Expected: CLI failure because the commands do not exist.

- [ ] **Step 3: Implement commands**

Add:

```python
@app.command("web")
def web(...) -> None:
    config = WebConfig.from_environment(...)
    uvicorn.run(create_app(config), host=config.host, port=config.port)


@app.command("worker")
def worker(...) -> None:
    service = EvaluationWorker(...)
    service.run_forever(poll_seconds=config.poll_seconds)
```

Handle SIGTERM between tasks. Never terminate a running evaluation from an HTTP request.

- [ ] **Step 4: Run CLI tests**

Run:

```bash
uv run --project evaluation pytest -c evaluation/pyproject.toml \
  evaluation/tests/web/test_cli.py evaluation/tests/unit/test_cli.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/pyproject.toml evaluation/uv.lock \
  evaluation/src/powercontext_eval/cli.py evaluation/tests/web/test_cli.py
git commit -m "feat(eval): add console service commands"
```

## Task 8: Scaffold the React application and typed API client

**Files:**
- Create: `evaluation/web/package.json`
- Create: `evaluation/web/package-lock.json`
- Create: `evaluation/web/tsconfig.json`
- Create: `evaluation/web/vite.config.ts`
- Create: `evaluation/web/index.html`
- Create: `evaluation/web/src/main.tsx`
- Create: `evaluation/web/src/types.ts`
- Create: `evaluation/web/src/api.ts`
- Create: `evaluation/web/src/test/setup.ts`
- Create: `evaluation/web/src/api.test.ts`

- [ ] **Step 1: Create the package manifest and test tooling**

Use React, TypeScript, Vite, Vitest, Testing Library, MSW, and Playwright. Scripts:

```json
{
  "dev": "vite",
  "build": "tsc -b && vite build",
  "test": "vitest run",
  "test:watch": "vitest",
  "e2e": "playwright test"
}
```

- [ ] **Step 2: Write failing API client tests**

Test typed success and fixed error parsing:

```typescript
it("creates a queued task", async () => {
  const result = await api.createTask(validTask);
  expect(result.status).toBe("queued");
  expect(result.queue_position).toBe(1);
});
```

Test event subscription reconnects only for non-terminal tasks and closes on a terminal event.

- [ ] **Step 3: Run and verify RED**

Run:

```bash
npm --prefix evaluation/web install
npm --prefix evaluation/web test -- --run src/api.test.ts
```

Expected: failure because `src/api.ts` does not exist or exports no client.

- [ ] **Step 4: Implement exact shared types and API client**

Mirror API response names exactly in `types.ts`. Implement `EvaluationApi` using relative `/api` URLs, JSON content
type checks, `AbortSignal`, and `EventSource` for task events. Do not accept arbitrary base URLs from browser query
parameters.

- [ ] **Step 5: Run frontend client tests and build**

Run:

```bash
npm --prefix evaluation/web test -- --run src/api.test.ts
npm --prefix evaluation/web run build
```

Expected: tests pass and Vite writes `evaluation/web/dist`.

- [ ] **Step 6: Commit**

```bash
git add evaluation/web
git commit -m "feat(eval): scaffold evaluation console frontend"
```

## Task 9: Build the desktop workbench and task queue UI

**Files:**
- Create: `evaluation/web/src/App.tsx`
- Create: `evaluation/web/src/components/AppShell.tsx`
- Create: `evaluation/web/src/components/TaskForm.tsx`
- Create: `evaluation/web/src/components/TaskList.tsx`
- Create: `evaluation/web/src/components/TaskDetail.tsx`
- Create: `evaluation/web/src/styles.css`
- Create: `evaluation/web/src/App.test.tsx`
- Create: `evaluation/web/src/components/TaskForm.test.tsx`
- Create: `evaluation/web/src/components/TaskList.test.tsx`

- [ ] **Step 1: Write failing component tests**

Cover:

- server capabilities populate every form control;
- default treatment is OFF/ON;
- submit sends exactly the selected validated values;
- returned task ID and queue position are visible;
- one running task is highlighted;
- queued, interrupted, failed, cancelled, and succeeded labels are distinct in text, not color alone;
- keyboard users can navigate and submit;
- no fabricated progress percentage appears.

Example:

```typescript
expect(screen.getByRole("heading", { name: "评测工作台" })).toBeVisible();
expect(screen.getByLabelText("测试方式")).toHaveValue("off_on");
expect(screen.queryByText(/%/)).not.toBeInTheDocument();
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
npm --prefix evaluation/web test -- --run \
  src/App.test.tsx src/components/TaskForm.test.tsx src/components/TaskList.test.tsx
```

Expected: failures because the components do not exist.

- [ ] **Step 3: Implement the app shell and routes**

Use an in-app route state for:

- `/` workbench;
- `/tasks` task list;
- `/tasks/:taskId` details;
- `/reports/:taskId` report.

Use semantic navigation links and browser history. No authentication UI or configuration editor.

- [ ] **Step 4: Implement task form and queue**

Disable submit only while the request itself is pending. After success, select and subscribe to the returned task.
Display queue position and current truthful phase label. Allow cancellation only when `status === "queued"`.

- [ ] **Step 5: Implement the selected desktop visual direction**

Use the approved wide workbench:

- fixed 188–216 pixel left navigation;
- compact top environment bar;
- two-column content area on desktop;
- task form on the left;
- current task or latest report on the right;
- responsive stacking below 960 pixels as a fallback, not the primary layout.

Use CSS variables, system fonts, visible focus states, AA contrast, and no decorative gradients.

- [ ] **Step 6: Run component tests and build**

Run:

```bash
npm --prefix evaluation/web test -- --run
npm --prefix evaluation/web run build
```

Expected: all frontend tests pass and production build succeeds.

- [ ] **Step 7: Commit**

```bash
git add evaluation/web/src
git commit -m "feat(eval): build desktop evaluation workbench"
```

## Task 10: Build the structured acceptance report

**Files:**
- Create: `evaluation/web/src/components/ReportView.tsx`
- Create: `evaluation/web/src/components/ReportView.test.tsx`
- Modify: `evaluation/web/src/App.tsx`
- Modify: `evaluation/web/src/styles.css`

- [ ] **Step 1: Write failing report tests**

Cover:

- final validity and OFF/ON resolution;
- missing metrics as `N/A`;
- signed deltas and percentages;
- treatment evidence;
- revision and configuration metadata;
- invalid treatment prevents a “passed” acceptance label;
- raw Markdown opens as plain text;
- injected HTML is displayed as text, never executed.

Example:

```typescript
render(<ReportView report={validReport} />);
expect(screen.getByText("验收有效")).toBeVisible();
expect(screen.getAllByText("RESOLVED")).toHaveLength(2);
expect(screen.getByText("−42.8%")).toBeVisible();
expect(screen.getByText("10 次 MCP 请求")).toBeVisible();
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
npm --prefix evaluation/web test -- --run src/components/ReportView.test.tsx
```

Expected: failure because `ReportView` does not exist.

- [ ] **Step 3: Implement the report hierarchy**

Render:

1. acceptance validity and task identity;
2. four primary comparison metrics;
3. side-by-side OFF/ON arm details;
4. treatment evidence;
5. reproducibility metadata;
6. a plain-text raw report link.

Use `Intl.NumberFormat` and one signed-delta formatter. Avoid interpreting Markdown as HTML.

- [ ] **Step 4: Run report and full frontend tests**

Run:

```bash
npm --prefix evaluation/web test -- --run
npm --prefix evaluation/web run build
```

Expected: all tests and build pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/web/src
git commit -m "feat(eval): render acceptance reports"
```

## Task 11: Add browser end-to-end acceptance

**Files:**
- Create: `evaluation/web/playwright.config.ts`
- Create: `evaluation/web/e2e/console.spec.ts`
- Create: `evaluation/tests/web/fake_runner_app.py`

- [ ] **Step 1: Write the failing desktop browser scenario**

At a 1440×1000 viewport:

```typescript
test("submits a task and opens its generated report", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("PowerContext 版本").fill("latest");
  await page.getByRole("button", { name: "开始评测" }).click();
  await expect(page.getByText("排队中 · 第 1 位")).toBeVisible();
  await expect(page.getByText("验收有效")).toBeVisible();
  await expect(page.getAllByText("RESOLVED")).toHaveCount(2);
});
```

Add a second scenario that submits two tasks and verifies one is running while the other remains queued.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
npm --prefix evaluation/web run e2e
```

Expected: failure because the E2E server fixture and completed UI flow are not configured.

- [ ] **Step 3: Add a deterministic fake-runner service**

The test-only service uses the real API, SQLite store, worker, frontend build, and a fake injected runner that emits
all phases and writes a valid report fixture. It must not add a production endpoint or test mode selected from an
HTTP request.

- [ ] **Step 4: Run E2E at desktop and narrow fallback sizes**

Run:

```bash
npm --prefix evaluation/web run e2e
```

Expected: all Playwright scenarios pass without console errors, uncaught exceptions, clipping, or horizontal
overflow at 1440×1000 and 900×900.

- [ ] **Step 5: Commit**

```bash
git add evaluation/web/playwright.config.ts evaluation/web/e2e \
  evaluation/tests/web/fake_runner_app.py
git commit -m "test(eval): cover console browser workflow"
```

## Task 12: Add deployment assets and operator documentation

**Files:**
- Create: `evaluation/deploy/powercontext-eval-web.service`
- Create: `evaluation/deploy/powercontext-eval-worker.service`
- Create: `evaluation/deploy/powercontext-eval.env.example`
- Create: `evaluation/README.md`

- [ ] **Step 1: Write systemd units**

Both units use:

```ini
WorkingDirectory=/data/powercontext-eval/deploy/powercontext
EnvironmentFile=/data/powercontext-eval/config/evaluation-console.env
```

The Web unit runs:

```text
/data/powercontext-eval/bin/uv run --project evaluation powercontext-eval web
```

The worker unit runs:

```text
/data/powercontext-eval/bin/uv run --project evaluation powercontext-eval worker
```

Use the existing m0 user, restart on failure, a private temporary directory, and conservative filesystem
protection that still permits the configured evaluation root and Docker access. Do not configure either unit to
manage new-api, MySQL, Redis, or the proxy.

- [ ] **Step 2: Document exact operation**

Document:

- local Python and frontend checks;
- configuration keys and safe defaults;
- build and deployment paths;
- service installation and rollback;
- health endpoint;
- queue semantics and restart behavior;
- artifact locations;
- secret and cleanup audits;
- real m0 acceptance procedure.

- [ ] **Step 3: Validate unit syntax and documentation commands**

Run locally:

```bash
systemd-analyze verify evaluation/deploy/powercontext-eval-web.service \
  evaluation/deploy/powercontext-eval-worker.service
```

If macOS lacks `systemd-analyze`, run the same command on m0 before installation. Run every platform-independent
command from the README.

- [ ] **Step 4: Commit**

```bash
git add evaluation/deploy evaluation/README.md
git commit -m "docs(eval): add console deployment guide"
```

## Task 13: Run full local verification

**Files:**
- Modify only files required by failures, with a regression test first.

- [ ] **Step 1: Run all Python checks**

```bash
uv lock --project evaluation --locked
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests -m "not live" -q
uv run --project evaluation ruff check evaluation
uv run --project evaluation ruff format --check evaluation
uv run --directory evaluation ty check src tests
```

Expected: all commands pass.

- [ ] **Step 2: Run all frontend checks**

```bash
npm --prefix evaluation/web ci
npm --prefix evaluation/web test -- --run
npm --prefix evaluation/web run build
npm --prefix evaluation/web run e2e
```

Expected: all commands pass with no browser console errors.

- [ ] **Step 3: Run repository integrity checks**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors and no uncommitted source changes.

## Task 14: Deploy and accept on m0

**Files:**
- No uncommitted source changes.
- Deployment target: `/data/powercontext-eval/deploy/powercontext`
- Runtime configuration: `/data/powercontext-eval/config/evaluation-console.env`
- Services: `powercontext-eval-web.service`, `powercontext-eval-worker.service`

- [ ] **Step 1: Record m0 pre-deployment state**

Record the deployed Git SHA and status of new-api, MySQL, Redis, proxy, disk space, Docker, and the chosen console
port. Do not read or print credential contents.

- [ ] **Step 2: Transfer the exact committed source**

Use a Git bundle or reachable Git remote, update only the evaluation deployment checkout, verify a clean working
tree, install locked Python dependencies, run `npm ci`, and build the frontend.

- [ ] **Step 3: Install and start dedicated services**

Install the environment file with mode `0600`, verify both systemd units, start the Web service, then start the
worker. Confirm `/api/health` reports the Web process, worker lease, and queue state.

- [ ] **Step 4: Verify durable queue behavior before a long run**

Using the browser/API with a test runner fixture is not allowed in production. Instead enqueue two real tasks with
distinct idempotency keys, verify one becomes running and the other remains queued, then cancel the queued task so
only one paid evaluation proceeds.

- [ ] **Step 5: Complete one real browser-submitted evaluation**

From the desktop console submit the pinned SWE-bench Pro OFF/ON task. Verify the browser shows every truthful phase,
the final official OFF/ON result, treatment evidence, metrics, and raw Markdown.

- [ ] **Step 6: Verify restart persistence**

After the real task reaches a terminal state, restart only the Web service. Confirm the task list and report remain
available. Do not restart the worker during a live evaluation.

- [ ] **Step 7: Run credential and cleanup audits**

Scan Web API responses, SQLite text fields, and new run artifacts for high-entropy values derived from the Codex
auth file without printing those values. Verify zero hits. Confirm no evaluation containers or networks remain.

- [ ] **Step 8: Verify existing services remain healthy**

Confirm new-api, MySQL, Redis, and the proxy have the same healthy state recorded before deployment.

- [ ] **Step 9: Record the acceptance evidence**

Record:

- deployed Git SHA;
- console URL;
- real task ID;
- official OFF/ON resolution;
- treatment counts;
- report path and SHA-256;
- queue serialization evidence;
- service restart persistence evidence;
- credential scan counts;
- cleanup and existing-service health.

Do not mark the feature complete until every design completion criterion has direct evidence.
