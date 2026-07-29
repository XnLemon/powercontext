# PowerContext Batch Evaluation Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed single-instance smoke-test console with a durable 731-task SWE-bench Pro OFF/ON batch
runner and a three-level report that moves from aggregate facts to filtered task pairs to a complete observable
context timeline.

**Architecture:** A pinned dataset catalog expands one immutable batch request into 731 durable paired tasks in
SQLite. The existing single worker still executes only one physical OFF/ON pair at a time, but each pair now receives
its own official dataset row and task image. Aggregate APIs derive counts and token totals from immutable child
results; context trace artifacts merge timestamped Codex JSONL events with evaluation-only PowerContext hook
injection records. React renders the approved overview, task-report, and contextual task-detail routes.

**Tech Stack:** Python 3.11+, Pydantic, SQLite, FastAPI, Typer, pytest, React 19, TypeScript, Zod, Vitest, Playwright,
systemd on m0.

---

## File Map

New focused modules:

- `evaluation/src/powercontext_eval/benchmarks/swebench_pro/catalog.py` — validate and index the pinned 731-row
  public dataset and normalize official fields.
- `evaluation/src/powercontext_eval/web/batches.py` — batch request/result models and pure aggregate calculations.
- `evaluation/src/powercontext_eval/context_trace.py` — normalized context event models and safe artifact parsing.
- `evaluation/scripts/record_codex_jsonl.py` — timestamp each Codex JSONL event while preserving its stdout bytes.
- `evaluation/web/src/components/BatchOverview.tsx` — aggregate facts and clickable pair distribution.
- `evaluation/web/src/components/BatchTaskReport.tsx` — filterable, searchable, sortable task-pair table.
- `evaluation/web/src/components/TaskRunDetail.tsx` — task statement, official tests, OFF/ON timeline, event detail.

Existing modules with expanded responsibility:

- `evaluation/src/powercontext_eval/benchmarks/swebench_pro/adapter.py` — one normalized official public row.
- `evaluation/src/powercontext_eval/benchmarks/swebench_pro/evaluator.py` — retain official test details.
- `evaluation/src/powercontext_eval/runner.py` — run any catalog instance, not one constant.
- `evaluation/src/powercontext_eval/powercontext_sut.py` — use the resolved per-instance image and retain trace data.
- `integrations/codex/plugins/powercontext/hooks/recall.py` — optional evaluation-only injection audit record.
- `evaluation/src/powercontext_eval/web/models.py` — task/batch/report API contracts.
- `evaluation/src/powercontext_eval/web/store.py` — schema migration, atomic batch expansion, child filtering.
- `evaluation/src/powercontext_eval/web/worker.py` — resolve one batch revision and execute arbitrary child instances.
- `evaluation/src/powercontext_eval/web/reporting.py` — per-task and aggregate validated projections.
- `evaluation/src/powercontext_eval/web/api.py` — batch/report/task/context endpoints.
- `evaluation/web/src/api.ts`, `types.ts`, `App.tsx`, `styles.css` — new API client, navigation, and views.

## Task 1: Pinned Full-Dataset Catalog

**Files:**
- Create: `evaluation/src/powercontext_eval/benchmarks/swebench_pro/catalog.py`
- Modify: `evaluation/src/powercontext_eval/benchmarks/swebench_pro/adapter.py`
- Modify: `evaluation/src/powercontext_eval/web/config.py`
- Test: `evaluation/tests/contract/test_swebench_catalog.py`
- Test fixture: `evaluation/tests/contract/fixtures/swebench_pro_public_v2.jsonl`

- [ ] **Step 1: Write failing catalog tests**

Cover the official mixed encoding of `FAIL_TO_PASS` and `PASS_TO_PASS`, unique IDs, deterministic source order,
Docker Hub image translation, exact file hash/count enforcement, and lookup without rereading the file.

```python
def test_catalog_loads_pinned_rows_in_source_order(tmp_path: Path) -> None:
    dataset = fixture_dataset(tmp_path)
    catalog = SweBenchProCatalog.load(
        dataset,
        expected_sha256=fixture_sha256(dataset),
        expected_count=3,
    )
    assert catalog.instance_ids == (
        "instance_owner__repo-a",
        "instance_owner__repo-b",
        "instance_owner__repo-c",
    )
    first = catalog.require("instance_owner__repo-a")
    assert first.fail_to_pass == ("test_fix",)
    assert first.pass_to_pass == ("test_regression",)
    assert first.task_image == "jefzda/sweap-images:owner.repo-owner__repo-a"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --project evaluation pytest evaluation/tests/contract/test_swebench_catalog.py -q
```

Expected: collection fails because `catalog.py` does not exist.

- [ ] **Step 3: Implement the immutable catalog**

Use these public contracts:

```python
PUBLIC_V2_COUNT = 731
PUBLIC_V2_SHA256 = "b5b2462bfbf5aeb2cb7ba7d215778a1768b85f9d7ad7f748546c7f80a0ad1510"
PUBLIC_V2_TASK_SET = "swebench-pro-public-v2"

@dataclass(frozen=True)
class SweBenchProCatalog:
    dataset_path: Path
    dataset_sha256: str
    instances: Mapping[str, SweBenchProInstance]
    instance_ids: tuple[str, ...]

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        expected_sha256: str = PUBLIC_V2_SHA256,
        expected_count: int = PUBLIC_V2_COUNT,
    ) -> Self: ...

    def require(self, instance_id: str) -> SweBenchProInstance: ...
```

Normalize test lists whether the row contains a JSON array or a JSON-encoded array string. Reject blank, duplicate,
missing, non-object, hash-mismatched, or count-mismatched datasets. Convert
`.../sweap-images/<repository>:<tag>` to `jefzda/sweap-images:<repository>-<tag>` without accepting arbitrary
registry paths.

Replace `WebConfig.raw_sample_path` with absolute `dataset_path`, defaulting to:

```text
<root>/cache/swebench-pro.git/helper_code/sweap_eval_full_v2.jsonl
```

- [ ] **Step 4: Run catalog/config tests and verify GREEN**

```bash
uv run --project evaluation pytest \
  evaluation/tests/contract/test_swebench_catalog.py \
  evaluation/tests/web/test_config.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/powercontext_eval/benchmarks/swebench_pro \
  evaluation/src/powercontext_eval/web/config.py \
  evaluation/tests/contract/fixtures/swebench_pro_public_v2.jsonl \
  evaluation/tests/contract/test_swebench_catalog.py \
  evaluation/tests/web/test_config.py
git commit -m "feat(eval): load pinned SWE-bench Pro task set"
```

## Task 2: Generalize the Per-Instance Runner

**Files:**
- Modify: `evaluation/src/powercontext_eval/runner.py`
- Modify: `evaluation/src/powercontext_eval/powercontext_sut.py`
- Modify: `evaluation/src/powercontext_eval/cli.py`
- Test: `evaluation/tests/unit/test_runner_phases.py`
- Test: `evaluation/tests/contract/test_codex_contract.py`
- Test: `evaluation/tests/unit/test_cli.py`

- [ ] **Step 1: Write failing arbitrary-instance tests**

```python
def test_runner_uses_catalog_instance_id_prompt_image_and_base_commit(...) -> None:
    instance = catalog.require("instance_owner__repo-b")
    result = run_swebench_pro_instance(config, instance=instance)
    assert retained_manifest["instance_id"] == instance.instance_id
    assert retained_manifest["task_image"] == instance.task_image
    assert observed_diff_base == instance.base_commit
    assert instance.problem_statement in observed_prompt
```

Also assert OFF and ON use one locally resolved immutable image ID, Gold uses the same dataset row, and no
`INSTANCE_ID`, `TASK_IMAGE`, or single-line dataset check remains in the execution path.

- [ ] **Step 2: Run and verify RED**

```bash
uv run --project evaluation pytest \
  evaluation/tests/unit/test_runner_phases.py \
  evaluation/tests/contract/test_codex_contract.py \
  evaluation/tests/unit/test_cli.py -q
```

Expected: arbitrary instances are rejected by the current fixed constants.

- [ ] **Step 3: Implement the generic runner**

Introduce:

```python
@dataclass(frozen=True)
class RunConfig:
    root: Path
    powercontext_source: Path
    powercontext_ref: str
    harness_root: Path
    harness_python: Path
    codex_binary: Path
    uv_binary: Path
    auth_json: Path
    proxy_url: str
    run_id: str

def run_swebench_pro_instance(
    config: RunConfig,
    *,
    instance: SweBenchProInstance,
    on_phase: PhaseCallback | None = None,
) -> RunResult: ...
```

Before Gold, pull `instance.task_image`, inspect its immutable local image ID, persist that ID in `manifest.json`,
and pass the image ID—not the mutable tag—to both OFF and ON. Retain the exact official row as `instance.jsonl`.
Keep the old CLI command as a compatibility wrapper that now requires `--instance-id` and resolves it from the
catalog.

- [ ] **Step 4: Run and verify GREEN**

Run the selected tests from Step 2. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/powercontext_eval/runner.py \
  evaluation/src/powercontext_eval/powercontext_sut.py \
  evaluation/src/powercontext_eval/cli.py \
  evaluation/tests/unit/test_runner_phases.py \
  evaluation/tests/contract/test_codex_contract.py \
  evaluation/tests/unit/test_cli.py
git commit -m "feat(eval): run arbitrary pinned benchmark instances"
```

## Task 3: Durable Batch and Child-Task Schema

**Files:**
- Create: `evaluation/src/powercontext_eval/web/batches.py`
- Modify: `evaluation/src/powercontext_eval/web/models.py`
- Modify: `evaluation/src/powercontext_eval/web/store.py`
- Test: `evaluation/tests/web/test_store.py`
- Test: `evaluation/tests/web/test_config.py`

- [ ] **Step 1: Write failing migration and atomic-expansion tests**

Tests must open a copy of the current schema, migrate it without deleting legacy task rows, create one three-instance
batch transactionally, replay its idempotency key, and prove that rollback leaves neither a batch nor children.

```python
def test_create_batch_expands_every_catalog_instance_atomically(store, catalog) -> None:
    batch, created = store.create_batch(request("batch-key"), catalog.instance_ids, now=NOW)
    assert created is True
    assert batch.total_tasks == 3
    assert [task.instance_id for task in store.list_batch_tasks(batch.batch_id)] == list(catalog.instance_ids)
    assert store.health_snapshot(now=NOW)["queued_tasks"] == 3
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run --project evaluation pytest evaluation/tests/web/test_store.py -q
```

Expected: `create_batch` and batch tables are absent.

- [ ] **Step 3: Add batch contracts and migration**

Use:

```python
class BatchCreate(FrozenModel):
    powercontext_ref: str
    benchmark: Literal["swebench-pro"]
    task_set: Literal["swebench-pro-public-v2"]
    model: Literal["gpt-5.6-sol"]
    reasoning_effort: Literal["medium"]
    treatment_mode: Literal["off_on"]
    idempotency_key: str

class PairCategory(StrEnum):
    OFF_FAIL_ON_PASS = "off_fail_on_pass"
    OFF_PASS_ON_FAIL = "off_pass_on_fail"
    BOTH_PASS = "both_pass"
    BOTH_FAIL = "both_fail"
    EXECUTION_FAILURE = "execution_failure"
```

Add a `batches` table and nullable `batch_id`, `instance_id`, and `source_index` columns to `tasks`. New batches use
foreign keys and unique `(batch_id, instance_id)` pairs. Preserve legacy tasks with `batch_id IS NULL`. Child task
IDs derive from batch sequence and source index and remain safe path components.

Batch status is derived from child states:

- queued: no child started;
- running: at least one child non-terminal or running;
- completed: every child terminal;
- cancelled: all children cancelled.

- [ ] **Step 4: Run store/config tests and verify GREEN**

```bash
uv run --project evaluation pytest \
  evaluation/tests/web/test_store.py \
  evaluation/tests/web/test_config.py -q
```

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/powercontext_eval/web/batches.py \
  evaluation/src/powercontext_eval/web/models.py \
  evaluation/src/powercontext_eval/web/store.py \
  evaluation/tests/web/test_store.py \
  evaluation/tests/web/test_config.py
git commit -m "feat(eval): persist complete evaluation batches"
```

## Task 4: Batch-Aware Serial Worker and Revision Pinning

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/worker.py`
- Modify: `evaluation/src/powercontext_eval/web/store.py`
- Test: `evaluation/tests/web/test_worker.py`

- [ ] **Step 1: Write failing worker tests**

Prove:

- `latest` resolves once for the batch and every child uses the same full SHA;
- the worker passes the child instance from the catalog to the generic runner;
- exactly one child is physically running across all batches;
- one failed child does not prevent later children from running;
- restart recovery preserves completed children and the pinned SHA.

```python
def test_latest_is_pinned_once_per_batch(worker, source, catalog) -> None:
    worker.run_once()
    worker.run_once()
    assert source.resolve_calls == 1
    assert runner.calls[0].powercontext_ref == f"commit:{SHA}"
    assert runner.calls[1].powercontext_ref == f"commit:{SHA}"
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run --project evaluation pytest evaluation/tests/web/test_worker.py -q
```

- [ ] **Step 3: Implement batch pinning and catalog lookup**

The first claimed child atomically pins `resolved_powercontext_sha` on its batch. Later workers must reuse it and
must reject a conflicting pin. `_run_config` receives `commit:<sha>`, while `run_swebench_pro_instance` receives the
catalog instance selected by the child's immutable `instance_id`.

Keep the existing SQLite lease and host `flock`; do not introduce parallel workers.

- [ ] **Step 4: Run and verify GREEN**

Run Step 2. Expected: all worker tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/powercontext_eval/web/worker.py \
  evaluation/src/powercontext_eval/web/store.py \
  evaluation/tests/web/test_worker.py
git commit -m "feat(eval): execute batch children serially"
```

## Task 5: Official Test-Level Results

**Files:**
- Modify: `evaluation/src/powercontext_eval/benchmarks/swebench_pro/evaluator.py`
- Modify: `evaluation/src/powercontext_eval/report.py`
- Modify: `evaluation/src/powercontext_eval/runner.py`
- Test: `evaluation/tests/contract/test_swebench_contract.py`
- Test: `evaluation/tests/unit/test_report.py`

- [ ] **Step 1: Write failing official-detail tests**

Given required F2P/P2P names and an official `*_output.json`, require:

```python
assert result.resolved is False
assert result.patch_applied is True
assert result.fail_to_pass == TestGroupResult(passed=0, total=1, failed=("TestLoad",))
assert result.pass_to_pass == TestGroupResult(passed=12, total=12, failed=())
```

Malformed or ambiguous output remains `OfficialResultError`, not ordinary unresolved.

- [ ] **Step 2: Run and verify RED**

```bash
uv run --project evaluation pytest \
  evaluation/tests/contract/test_swebench_contract.py \
  evaluation/tests/unit/test_report.py -q
```

- [ ] **Step 3: Retain strict evaluator details**

Extend `OfficialEvaluation` with:

```python
@dataclass(frozen=True)
class TestGroupResult:
    passed: int
    total: int
    failed: tuple[str, ...]

@dataclass(frozen=True)
class OfficialEvaluation:
    instance_id: str
    resolved: bool
    patch_applied: bool
    fail_to_pass: TestGroupResult
    pass_to_pass: TestGroupResult
    log_excerpt: str | None
    raw_stdout: str
    raw_stderr: str
```

Load only the exact instance/prefix output file beneath the evaluator output directory, validate every test object,
compare parsed test names against normalized required groups, and retain a bounded plain-text excerpt. Persist this
data in `report.json`; remove duplicate `passed` and patch-byte fields from the new projection.

- [ ] **Step 4: Run and verify GREEN**

Run Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/powercontext_eval/benchmarks/swebench_pro/evaluator.py \
  evaluation/src/powercontext_eval/report.py \
  evaluation/src/powercontext_eval/runner.py \
  evaluation/tests/contract/test_swebench_contract.py \
  evaluation/tests/unit/test_report.py
git commit -m "feat(eval): retain official test-level outcomes"
```

## Task 6: Timestamped Codex and PowerContext Injection Trace

**Files:**
- Create: `evaluation/scripts/record_codex_jsonl.py`
- Create: `evaluation/src/powercontext_eval/context_trace.py`
- Modify: `evaluation/src/powercontext_eval/codex.py`
- Modify: `evaluation/src/powercontext_eval/powercontext_sut.py`
- Modify: `integrations/codex/plugins/powercontext/hooks/recall.py`
- Test: `evaluation/tests/unit/test_context_trace.py`
- Test: `evaluation/tests/contract/test_codex_contract.py`
- Test: `tests/codex_plugin/test_recall.py`

- [ ] **Step 1: Write failing trace tests**

Test that the recorder preserves each original stdout JSON line byte-for-byte while appending UTC timestamps and a
monotonic sequence to a sidecar. Test the hook with evaluation tracing enabled and disabled. Test merged ordering and
secret rejection.

```python
def test_hook_records_exact_injected_context_only_when_eval_trace_enabled(...) -> None:
    main(settings)
    event = json.loads(trace_path.read_text().splitlines()[0])
    assert event["event_type"] == "powercontext_injection"
    assert event["query"] == "fix namespace refresh"
    assert event["injected_text"].startswith("PowerContext recalled")
    assert event["hits"][0]["citation"]["entry_id"] == "decision-1"
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run --project evaluation pytest \
  evaluation/tests/unit/test_context_trace.py \
  evaluation/tests/contract/test_codex_contract.py \
  tests/codex_plugin/test_recall.py -q
```

- [ ] **Step 3: Implement evaluation-only trace capture**

`record_codex_jsonl.py` runs the exact Codex argv after `--`, forwards stdin/stderr, preserves stdout, and writes one
sidecar envelope per JSONL line:

```json
{"sequence":3,"observed_at":"2026-07-29T08:10:11.123456Z","event":{...}}
```

The hook writes `powercontext_injection` records only when
`POWERCONTEXT_EVAL_TRACE_PATH=/runtime/pc-home/evaluation-injections.jsonl` is set by the isolated evaluator. Open
that path with append/no-follow semantics and mode `0600`; default product use writes no trace. Record the query,
returned hit citation/text/score/matched channels, exact rendered additional context, scope, session/turn IDs, and
UTC timestamp.

`context_trace.py` merges:

- original benchmark prompt;
- timestamped Codex events;
- injection events;
- official-evaluation event.

It emits stable sequence numbers with explicit actor/event type and full sanitized input/output. Copy the merged
JSONL through `ArtifactStore(forbidden_values=...)`; any credential match fails closed.

- [ ] **Step 4: Run and verify GREEN**

Run Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/scripts/record_codex_jsonl.py \
  evaluation/src/powercontext_eval/context_trace.py \
  evaluation/src/powercontext_eval/codex.py \
  evaluation/src/powercontext_eval/powercontext_sut.py \
  integrations/codex/plugins/powercontext/hooks/recall.py \
  evaluation/tests/unit/test_context_trace.py \
  evaluation/tests/contract/test_codex_contract.py \
  tests/codex_plugin/test_recall.py
git commit -m "feat(eval): capture ordered context injection traces"
```

## Task 7: Aggregate, Task-List, Detail, and Timeline APIs

**Files:**
- Modify: `evaluation/src/powercontext_eval/web/batches.py`
- Modify: `evaluation/src/powercontext_eval/web/models.py`
- Modify: `evaluation/src/powercontext_eval/web/reporting.py`
- Modify: `evaluation/src/powercontext_eval/web/api.py`
- Modify: `evaluation/src/powercontext_eval/web/store.py`
- Test: `evaluation/tests/web/test_reporting.py`
- Test: `evaluation/tests/web/test_api.py`

- [ ] **Step 1: Write failing API tests**

Cover:

- create/list/get/cancel batch;
- aggregate reconciliation;
- pair-category filters;
- repository/task search;
- token-delta sorting and stable pagination;
- detail official tests;
- OFF/ON context-event pagination and full event content;
- legacy single-task report remains readable but does not appear as a batch;
- symlink, traversal, oversized artifact, malformed JSONL, and secret-shaped field rejection.

```python
def test_batch_report_reconciles_pair_categories_and_tokens(client) -> None:
    report = client.get(f"/api/batches/{BATCH}/report").json()
    assert report["total_tasks"] == 4
    assert report["off"]["resolved"] == 2
    assert report["on"]["resolved"] == 2
    assert sum(report["pair_categories"].values()) == report["comparable_pairs"]
    assert report["tokens"]["total"]["on"] == report["tokens"]["input"]["on"] + report["tokens"]["output"]["on"]
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run --project evaluation pytest \
  evaluation/tests/web/test_reporting.py \
  evaluation/tests/web/test_api.py -q
```

- [ ] **Step 3: Implement new same-origin endpoints**

```text
POST /api/batches
GET  /api/batches
GET  /api/batches/{batch_id}
POST /api/batches/{batch_id}/cancel
GET  /api/batches/{batch_id}/events
GET  /api/batches/{batch_id}/report
GET  /api/batches/{batch_id}/tasks
GET  /api/batches/{batch_id}/tasks/{task_id}
GET  /api/batches/{batch_id}/tasks/{task_id}/context/{arm}
GET  /api/batches/{batch_id}/tasks/{task_id}/context/{arm}/{sequence}
```

The aggregate response contains only facts:

```python
class BatchReportResponse(FrozenModel):
    batch_id: str
    total_tasks: int
    comparable_pairs: int
    execution_failures: int
    off: ResolutionAggregate
    on: ResolutionAggregate
    resolution_rate_delta_points: float
    pair_categories: Mapping[PairCategory, int]
    tokens: TokenAggregate
    revisions: Mapping[str, str]
    configuration: Mapping[str, str]
```

Do not return `acceptance_valid`, lifecycle, treatment validity, patch bytes, or absent elapsed-time rows in the new
batch report.

- [ ] **Step 4: Run and verify GREEN**

Run Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/src/powercontext_eval/web \
  evaluation/tests/web/test_reporting.py \
  evaluation/tests/web/test_api.py
git commit -m "feat(eval): expose batch drill-down report APIs"
```

## Task 8: Frontend Contracts and Report Navigation

**Files:**
- Modify: `evaluation/web/src/types.ts`
- Modify: `evaluation/web/src/api.ts`
- Modify: `evaluation/web/src/App.tsx`
- Modify: `evaluation/web/src/components/AppShell.tsx`
- Modify: `evaluation/web/src/components/TaskForm.tsx`
- Test: `evaluation/web/src/api.test.ts`
- Test: `evaluation/web/src/App.test.tsx`
- Test: `evaluation/web/src/components/TaskForm.test.tsx`

- [ ] **Step 1: Write failing frontend contract/navigation tests**

Assert the form creates one full task-set batch rather than selecting an instance. Assert the sidebar contains only
“总体报告” and “任务详细报告”. Assert task detail is unreachable without a selected task and highlights the task-report
navigation item when open.

```tsx
expect(screen.getByRole("button", { name: "运行完整评测" })).toBeVisible();
expect(screen.queryByLabelText("测试实例")).not.toBeInTheDocument();
expect(screen.queryByRole("button", { name: "单任务详情" })).not.toBeInTheDocument();
```

- [ ] **Step 2: Run and verify RED**

```bash
npm --prefix evaluation/web test -- --run \
  src/api.test.ts src/App.test.tsx src/components/TaskForm.test.tsx
```

- [ ] **Step 3: Implement strict Zod contracts and contextual routing**

Replace task-creation UI with `BatchCreate`. Keep browser-local routes:

```text
report/:batchId
report/:batchId/tasks?category=...
report/:batchId/tasks/:taskId
```

Use the History API without adding a router dependency. Preserve filter/search/sort in the URL query and breadcrumb.
Task detail has no global navigation entry.

- [ ] **Step 4: Run and verify GREEN**

Run Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/web/src/types.ts evaluation/web/src/api.ts \
  evaluation/web/src/App.tsx evaluation/web/src/components/AppShell.tsx \
  evaluation/web/src/components/TaskForm.tsx \
  evaluation/web/src/api.test.ts evaluation/web/src/App.test.tsx \
  evaluation/web/src/components/TaskForm.test.tsx
git commit -m "feat(eval): navigate complete batch reports"
```

## Task 9: 总体报告 and 任务详细报告 Pages

**Files:**
- Create: `evaluation/web/src/components/BatchOverview.tsx`
- Create: `evaluation/web/src/components/BatchTaskReport.tsx`
- Modify: `evaluation/web/src/styles.css`
- Test: `evaluation/web/src/components/BatchOverview.test.tsx`
- Test: `evaluation/web/src/components/BatchTaskReport.test.tsx`
- Modify: `evaluation/web/src/components/ReportIndex.tsx`

- [ ] **Step 1: Write failing rendering and interaction tests**

Use a reconciled 100-task fixture with 41 OFF, 48 ON, 14 positive flips, 7 negative flips, 34 both-pass, and 45
both-fail. Assert:

- all counts/rates/token totals render;
- no authored conclusion or acceptance badge renders;
- clicking 7 negative flips opens the exact filter;
- only rows matching each filter render;
- search and token sort update the URL;
- `N/A`, patch size, lifecycle, and treatment-validity text never render.

- [ ] **Step 2: Run and verify RED**

```bash
npm --prefix evaluation/web test -- --run \
  src/components/BatchOverview.test.tsx \
  src/components/BatchTaskReport.test.tsx
```

- [ ] **Step 3: Implement the approved desktop pages**

Match the approved prototype:

- overview identity and four objective KPI cards;
- clickable four-cell pair distribution;
- input/output/total Token totals with explicit denominators;
- task report filters, search, sort, stable pagination;
- no single-task detail entry in the sidebar.

Omit an entire metric row when the server returns it as absent.

- [ ] **Step 4: Run and verify GREEN**

Run Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/web/src/components/BatchOverview.tsx \
  evaluation/web/src/components/BatchTaskReport.tsx \
  evaluation/web/src/components/BatchOverview.test.tsx \
  evaluation/web/src/components/BatchTaskReport.test.tsx \
  evaluation/web/src/components/ReportIndex.tsx \
  evaluation/web/src/styles.css
git commit -m "feat(eval): render aggregate and task batch reports"
```

## Task 10: 单任务详情 and Context Timeline

**Files:**
- Create: `evaluation/web/src/components/TaskRunDetail.tsx`
- Create: `evaluation/web/src/components/ContextTimeline.tsx`
- Modify: `evaluation/web/src/styles.css`
- Test: `evaluation/web/src/components/TaskRunDetail.test.tsx`
- Test: `evaluation/web/src/components/ContextTimeline.test.tsx`

- [ ] **Step 1: Write failing task-detail tests**

Assert:

- complete problem statement expands in place;
- official patch-apply, F2P, P2P, failed tests, and final resolution render once;
- OFF/ON timeline switch preserves the selected task;
- events render in server sequence order;
- selecting an injection shows timestamp, target turn, query, citations, exact injected text, and token contribution;
- large event content loads all chunks in order;
- browser never renders event HTML as executable markup.

- [ ] **Step 2: Run and verify RED**

```bash
npm --prefix evaluation/web test -- --run \
  src/components/TaskRunDetail.test.tsx \
  src/components/ContextTimeline.test.tsx
```

- [ ] **Step 3: Implement the contextual detail page**

Use a two-column desktop layout:

- left: OFF/ON chronological timeline;
- right: lazy-loaded selected event input/output;
- top: immutable task statement, official result, and token comparison.

PowerContext injection events use one stable visual marker and remain at their real sequence position. Use
`textContent`/React text rendering only; do not use `dangerouslySetInnerHTML`.

- [ ] **Step 4: Run and verify GREEN**

Run Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add evaluation/web/src/components/TaskRunDetail.tsx \
  evaluation/web/src/components/ContextTimeline.tsx \
  evaluation/web/src/components/TaskRunDetail.test.tsx \
  evaluation/web/src/components/ContextTimeline.test.tsx \
  evaluation/web/src/styles.css
git commit -m "feat(eval): inspect task context timelines"
```

## Task 11: End-to-End Batch Workflow

**Files:**
- Modify: `evaluation/tests/web/fake_runner_app.py`
- Modify: `evaluation/web/e2e/console.e2e.spec.ts`
- Modify: `evaluation/web/playwright.config.ts`

- [ ] **Step 1: Extend the fake runner to a deterministic multi-task batch**

The fixture must create at least six children spanning all four pair categories plus an execution failure. It writes
official test details and OFF/ON context events with at least two ON injections.

- [ ] **Step 2: Write failing Playwright scenarios**

Cover:

1. submit a full batch;
2. observe one child running and the rest queued;
3. wait for aggregate completion;
4. verify aggregate reconciliation and token totals;
5. click negative-flip count;
6. verify only matching task rows;
7. open a row;
8. switch OFF/ON;
9. inspect exact injection content;
10. navigate back and retain the negative filter;
11. reload every page and retain server-backed state;
12. assert zero console and page errors at 1440 and 960 pixel desktop widths.

- [ ] **Step 3: Run and verify RED, then implement fixture support**

```bash
npm --prefix evaluation/web run test:e2e
```

Expected before fixture support: batch route assertions fail. Expected after support: all scenarios pass.

- [ ] **Step 4: Commit**

```bash
git add evaluation/tests/web/fake_runner_app.py \
  evaluation/web/e2e/console.e2e.spec.ts \
  evaluation/web/playwright.config.ts
git commit -m "test(eval): cover complete batch report workflow"
```

## Task 12: Documentation, Migration, and Full Verification

**Files:**
- Modify: `evaluation/README.md`
- Modify: `evaluation/deploy/powercontext-eval.env.example`
- Modify: `docs/superpowers/specs/2026-07-29-evaluation-console-design.md`
- Modify: `docs/superpowers/specs/2026-07-29-batch-evaluation-report-design.md`
- Test: `evaluation/tests/web/test_deployment.py`

- [ ] **Step 1: Document the exact operational contract**

Document:

- pinned 731-row public task set and SHA-256;
- one batch = all 731 tasks × OFF/ON;
- one physical pair at a time;
- expected long duration and cost;
- restart/resume behavior;
- database migration and rollback backup;
- context retention and sanitization;
- report denominators and failure semantics;
- m0 deployment commands without credential values.

- [ ] **Step 2: Run the complete local verification matrix**

```bash
uv sync --project evaluation --frozen
uv lock --project evaluation --locked
uv run --project evaluation pytest evaluation/tests -q
uv run --project evaluation ruff check evaluation integrations/codex/plugins/powercontext/hooks tests/codex_plugin
uv run --project evaluation ruff format --check evaluation integrations/codex/plugins/powercontext/hooks tests/codex_plugin
uv run --project evaluation ty check
npm --prefix evaluation/web ci
npm --prefix evaluation/web test -- --run
npm --prefix evaluation/web run build
npm --prefix evaluation/web run test:e2e
git diff --check
git status --short
```

Expected: every command exits zero and the worktree is clean after the final commit.

- [ ] **Step 3: Deploy safely to m0**

Before mutation:

- capture exact new-api/MySQL/Redis IDs, health, and restart counts;
- back up the current deployment, database, units, and environment;
- verify the proxy and credential file without printing its contents.

Deploy the exact reviewed SHA, run schema migration, build/install the frontend, restart only
`powercontext-eval-web` and `powercontext-eval-worker`, and verify health.

- [ ] **Step 4: Validate without accidentally launching a paid 731-task batch**

Use the deterministic local/e2e batch for full UI behavior. On m0:

- validate the production catalog reports exactly 731 unique tasks and the pinned hash;
- create then cancel a production batch before any child is claimed only when the worker is temporarily stopped and
  the cancellation is observed transactionally;
- do not start the paid full batch without an explicit final user instruction after estimated duration/cost is
  visible.

Verify:

- batch survives Web restart;
- cancellation leaves zero running children;
- Web/Worker remain active;
- no evaluation containers or networks remain;
- sensitive-value scans over API, SQLite, logs, and artifacts return zero hits;
- existing m0 services retain their original IDs/restart counts/health.

- [ ] **Step 5: Commit**

```bash
git add evaluation/README.md evaluation/deploy/powercontext-eval.env.example \
  evaluation/tests/web/test_deployment.py \
  docs/superpowers/specs/2026-07-29-evaluation-console-design.md \
  docs/superpowers/specs/2026-07-29-batch-evaluation-report-design.md
git commit -m "docs(eval): operate complete batch reports"
```

## Completion Evidence

Implementation is complete only when all of the following are fresh and recorded:

1. catalog verification reports exactly 731 unique tasks and the pinned SHA-256;
2. one batch creates exactly 731 durable child task pairs;
3. the global worker never runs more than one pair concurrently;
4. batch revision and task-set provenance remain fixed after restart;
5. aggregate counts reconcile exactly with child records;
6. total Token equals input plus output over an explicit identical paired denominator;
7. every filter returns exactly its aggregate category;
8. task detail exposes official test evidence and the complete observable sanitized event sequence;
9. ON injection records identify exact timing, target turn, query, hits, and injected text;
10. the approved two-item global navigation and contextual task-detail route pass browser tests;
11. the complete local verification matrix passes;
12. m0 deployment, migration, security, cleanup, persistence, and existing-service checks pass;
13. a paid 731-task batch is not started until the user explicitly confirms after seeing its expected operational
    cost and duration.
