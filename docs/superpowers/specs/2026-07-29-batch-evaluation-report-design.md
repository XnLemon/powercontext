# PowerContext Batch Evaluation Report Design

**Date:** 2026-07-29  
**Status:** Approved visual direction; awaiting written-spec review  
**Supersedes:** The single-task report presentation in
`2026-07-29-evaluation-console-design.md`

## 1. Purpose

The evaluation console currently proves that one SWE-bench Pro instance can run through PowerContext OFF and ON
and produce an official result. That is an execution smoke test, not a product-effect evaluation.

The report must instead represent one complete, fixed evaluation batch:

- one PowerContext revision;
- one Codex model and reasoning configuration;
- one pinned benchmark dataset and harness revision;
- one selected task set;
- one OFF and one ON execution for every selected task.

The user reads the report in three levels:

1. inspect the complete batch's aggregate facts;
2. filter and compare individual tasks;
3. open a specific task and reconstruct its execution and injected context in chronological order.

The product must present evidence and measurements without authoring a qualitative conclusion for the user.

## 2. Product Principles

1. **A batch, not one task, is the report unit.** A single-task result is evidence inside a batch report.
2. **Correctness precedes efficiency.** Resolution results are primary. Token and elapsed-time data are secondary.
3. **Facts, not conclusions.** The UI does not label a batch as accepted, invalid, improved, degraded, or requiring
   attention.
4. **Paired comparison.** OFF and ON are two executions of the same task under the same pinned configuration.
5. **Traceability.** Every aggregate value links to the tasks that produced it.
6. **No meaningless placeholders.** A metric with no measured value is omitted instead of rendered as `N/A`.
7. **Internal states stay internal.** Artifact lifecycle, treatment-validity enums, and patch byte counts do not
   appear in the main report.

## 3. Report Navigation

The left navigation contains two stable destinations:

- **总体报告**
- **任务详细报告**

“单任务详情” is not a global navigation item. It exists only after the user selects a task from the task report.
While a task detail is open, “任务详细报告” remains highlighted. A breadcrumb returns the user to the same filtered
task list.

```text
总体报告
  └─ click an aggregate or result category
      └─ 任务详细报告 (matching filter retained)
          └─ click one task
              └─ 单任务详情
```

## 4. Page 1: 总体报告

### 4.1 Batch Identity

The header identifies the immutable batch:

- batch ID;
- benchmark and task-set revision;
- selected-task count;
- PowerContext revision;
- Codex version, model, and reasoning effort;
- creation and completion timestamps.

It does not show an “验收有效 / 无效” badge or an authored conclusion.

### 4.2 Primary Correctness Metrics

The first row contains:

- total selected tasks;
- OFF resolved count and resolution rate;
- ON resolved count and resolution rate;
- ON minus OFF resolution-rate difference in percentage points.

Both headline resolution rates use the total selected-task count as the denominator. An execution or evaluator
failure does not count as resolved, but its failure count remains visible so the user can distinguish product
failure from evaluation-infrastructure failure.

For example:

```text
总任务数       100
OFF 解决率     41 / 100 = 41%
ON 解决率      48 / 100 = 48%
解决率差值     +7 percentage points
```

The UI labels the last value “解决率差值”, not “提升”.

### 4.3 Pair Outcome Distribution

Every comparable task belongs to exactly one category:

| Category | Definition |
|---|---|
| OFF 未通过 / ON 通过 | OFF unresolved and ON resolved |
| OFF 通过 / ON 未通过 | OFF resolved and ON unresolved |
| OFF / ON 均通过 | both resolved |
| OFF / ON 均未通过 | both unresolved |

The four counts must sum to the comparable-pair count, which is displayed beside the distribution. Each category is
clickable and opens the task report with the corresponding filter.

Tasks missing an official result for either side are not silently placed in “未通过”. They are shown separately as
execution/evaluation failures and excluded from the four-cell paired distribution.

### 4.4 Resource Metrics

The aggregate page shows:

- OFF total input tokens;
- ON total input tokens;
- absolute and percentage difference;
- OFF total output tokens;
- ON total output tokens;
- absolute and percentage difference.

Totals use only task pairs for which both sides contain the relevant usage metric. The UI shows the denominator,
for example “98 / 100 pairs with complete token usage”.

Elapsed time is shown only after both arms reliably capture comparable wall-clock durations. Its purpose is to
measure efficiency when correctness is considered alongside it. Until those data exist, the entire elapsed-time
section is absent.

Patch byte count is excluded from the primary and task-level report because it is not a code-quality measure.

## 5. Page 2: 任务详细报告

### 5.1 Purpose

This page enumerates every selected task in the batch and lets the user locate the task pairs behind an aggregate.

### 5.2 Filters

The page supports:

- all tasks;
- OFF passed / ON failed;
- OFF failed / ON passed;
- both passed;
- both failed;
- execution or evaluation failure.

It also supports repository/task-ID search and sorting by token difference. A filter entered from the aggregate page
is preserved when navigating back from a task.

### 5.3 Table Columns

Each row contains:

- task ID and short task title;
- repository;
- OFF official result;
- ON official result;
- pair category;
- OFF total tokens;
- ON total tokens;
- absolute and percentage token difference;
- link to task detail.

Rows do not include artifact lifecycle, treatment-validity state, patch size, or unavailable elapsed time.

For a single task, “通过” means the official evaluator returned `resolved=true`; “未通过” means it returned
`resolved=false`. Execution and evaluator failures have their own status and are never mislabeled as ordinary
unresolved results.

## 6. Page 3: 单任务详情

### 6.1 Entry and Header

The page can only be opened from a concrete task row. Its header contains:

- complete instance ID and repository;
- OFF and ON official results;
- OFF and ON token totals and their difference;
- immutable batch configuration.

### 6.2 Original Task

The complete benchmark problem statement is available without truncation. A collapsed summary may be used initially,
but the user can expand the original text in place.

### 6.3 Official Evaluation Details

For each side, show:

- whether the patch applied successfully;
- FAIL_TO_PASS passed count and total;
- PASS_TO_PASS passed count and total;
- final `RESOLVED` / `UNRESOLVED` boolean;
- failed test names and a bounded evaluator-log excerpt when unresolved.

The official evaluator determines correctness by applying the generated patch in the pinned container and running
the benchmark's required tests. A resolved task must satisfy the official issue-resolution tests and regression
tests. The UI must not duplicate the same boolean as separate `RESOLVED` and `PASS` fields.

### 6.4 Chronological Context Timeline

OFF and ON each have a chronological timeline. The user can switch between them while remaining on the same task.
Every retained event has:

- globally ordered sequence number within the arm;
- wall-clock timestamp and elapsed offset;
- actor and event type;
- complete sanitized input;
- complete sanitized output;
- token usage when available;
- source artifact reference.

Event types include:

- original task and system instructions;
- model messages;
- file reads and searches;
- tool requests and responses;
- sub-agent or handoff events;
- PowerContext MCP requests and responses;
- PowerContext prompt/context injection;
- code modifications and test executions;
- final patch capture;
- official evaluation.

Selecting an event opens its full content in a detail panel. The sequence is reconstructable without relying on
visual position alone.

### 6.5 PowerContext Injection Events

ON injections are visually distinguishable but remain in their real chronological positions. Each injection shows:

- injection sequence;
- exact insertion timestamp;
- target model turn or prompt position;
- retrieval query;
- returned context items;
- exact sanitized injected text;
- source type and source identifier;
- provenance/version;
- input-token contribution when measurable.

This lets the user determine what PowerContext supplied, when it supplied it, and what Agent activity followed.
OFF contains no injection events.

## 7. Official Result Semantics

The current adapter invokes the pinned official SWE-bench Pro evaluator and reads the exact task boolean from
`eval_results.json`.

- `resolved=true` is displayed as **通过 / 已解决**.
- `resolved=false` is displayed as **未通过 / 未解决**.
- missing, malformed, ambiguous, or failed evaluator output is displayed as an **评测执行失败**, not as an ordinary
  task failure.

The official result is the per-task correctness score: resolved is 1 and unresolved is 0. The headline batch
resolution rate is the count of resolved tasks divided by the batch's total selected-task count. The paired-outcome
distribution separately uses only tasks for which both OFF and ON produced an official result.

## 8. Data Model

### 8.1 Batch

```text
EvaluationBatch
  id
  immutable configuration and revisions
  selected_task_ids[]
  state
  created_at / started_at / finished_at
  aggregate counters
```

### 8.2 Paired Task

```text
PairedTaskResult
  batch_id
  instance_id
  problem_statement
  repository
  off_run
  on_run
  pair_category
```

### 8.3 Arm Run

```text
ArmRun
  arm
  execution status
  official resolution
  official test details
  input/output token usage
  elapsed time when reliable
  ordered context events[]
```

### 8.4 Context Event

```text
ContextEvent
  sequence
  occurred_at
  elapsed_offset
  actor
  event_type
  input
  output
  token usage
  artifact reference
  injection metadata (ON injection events only)
```

Batch aggregates are derived from immutable per-task results. They are never maintained as an unrelated editable
summary.

## 9. Batch Execution and Report Publication

One batch expands into one paired task for each selected benchmark instance. The existing global constraint remains:
only one physical evaluation task runs at a time; remaining tasks queue durably.

The aggregate report is marked complete only when every selected task has reached a terminal state. Partial progress
can be viewed while a batch runs, but it is labeled as partial data and shows its denominator explicitly.

Report publication must tolerate process restart:

- batch membership is durable;
- completed task pairs are not rerun automatically;
- queue order is durable;
- aggregate values can be rebuilt from retained task artifacts;
- selecting a task always opens the exact OFF/ON artifacts used in the aggregate.

## 10. Security and Retention

“完整上下文” means the complete retained and sanitized Agent interaction, not raw secrets or unrestricted host logs.

Before persistence and API delivery, the system must remove or replace:

- Codex authentication material;
- proxy credentials;
- environment secrets;
- credential-bearing command output;
- other configured forbidden values.

The API returns structured context events rather than arbitrary executable HTML. Large content uses bounded,
paginated delivery without changing event order. Raw artifacts remain server-side and are referenced by immutable
IDs.

## 11. Acceptance Criteria

1. One report represents one fixed batch, not one task or mixed historical runs.
2. The aggregate counts reconcile with the task list and paired-outcome categories.
3. OFF and ON resolution rates use an explicit, identical denominator.
4. Clicking any aggregate category opens exactly the matching tasks.
5. Filters and search preserve state when returning from a task detail.
6. A task detail cannot be opened without a selected task.
7. The detail exposes the full sanitized task statement and ordered OFF/ON context events.
8. Every ON injection identifies when, where, why, and what was injected.
9. Official test details explain `RESOLVED` or `UNRESOLVED`.
10. Missing metrics are omitted; lifecycle, treatment-validity enums, and patch size are absent from the main UI.
11. The UI presents measurements without an authored acceptance or optimization conclusion.
12. Existing unauthenticated, single-worker, m0-only deployment constraints remain unchanged.
