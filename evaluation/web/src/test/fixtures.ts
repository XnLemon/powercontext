import { vi } from "vitest";

import type { EvaluationApi } from "../api";
import type { Capabilities, HealthResponse, ReportResponse, TaskCreate, TaskRecord, TaskSummary } from "../types";

export function deferred<T>(): {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(reason?: unknown): void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

export const instanceId =
  "instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9" as const;

export const capabilities: Capabilities = {
  benchmarks: ["swebench-pro"],
  instances: [instanceId],
  models: ["gpt-5.6-sol"],
  reasoning_efforts: ["medium"],
  treatment_modes: ["off_on"],
};

export const health: HealthResponse = {
  service: "ok",
  worker_lease_active: true,
  queued_tasks: 1,
  running_tasks: 1,
};

export const report: ReportResponse = {
  task_id: "task-report",
  acceptance_valid: true,
  off: {
    arm: "off",
    resolution: "unresolved",
    input_tokens: 1_963_221,
    output_tokens: null,
    elapsed_seconds: 125.55,
    patch_bytes: 1_024,
  },
  on: {
    arm: "on",
    resolution: "resolved",
    input_tokens: 1_122_207,
    output_tokens: 12_345,
    elapsed_seconds: 100,
    patch_bytes: 2_048,
  },
  comparison: {
    input_tokens: { off: 1_963_221, on: 1_122_207, delta: -841_014, percent: -42.839 },
    output_tokens: null,
    elapsed_seconds: { off: 125.55, on: 100, delta: -25.55, percent: -20.3505 },
    patch_bytes: { off: 1_024, on: 2_048, delta: 1_024, percent: 100 },
  },
  evidence: {
    off: {
      mcp_requests: 0,
      prompt_sources: 0,
      plugin_checkout_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      plugin_id: "powercontext",
      plugin_installed: true,
      plugin_version: "0.1.0",
      scope_id: "eval:run-123:off",
      server_ready: true,
    },
    on: {
      mcp_requests: 10,
      prompt_sources: 2,
      plugin_checkout_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      plugin_id: "powercontext",
      plugin_installed: true,
      plugin_version: "0.1.0",
      scope_id: "eval:run-123:on",
      server_ready: true,
    },
  },
  revisions: {
    powercontext: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    codex: "gpt-5.6-sol",
  },
  configuration: {
    model: "gpt-5.6-sol",
    reasoning_effort: "medium",
    run_id: "run-123",
  },
  generated_at: "2026-07-29T01:02:03Z",
};

export const request: TaskCreate = {
  powercontext_ref: "latest",
  benchmark: "swebench-pro",
  instance_id: instanceId,
  model: "gpt-5.6-sol",
  reasoning_effort: "medium",
  treatment_mode: "off_on",
  idempotency_key: "fixture-key",
};

export function summary(
  status: TaskSummary["status"],
  taskId = `task-${status}`,
  overrides: Partial<TaskSummary> = {},
): TaskSummary {
  return {
    task_id: taskId,
    powercontext_ref: "latest",
    instance_id: instanceId,
    model: "gpt-5.6-sol",
    status,
    phase: status === "running" ? "running_off" : null,
    created_at: "2026-07-29T01:00:00Z",
    started_at: status === "queued" || status === "cancelled" ? null : "2026-07-29T01:01:00Z",
    finished_at: ["succeeded", "failed", "interrupted", "cancelled"].includes(status)
      ? "2026-07-29T01:02:00Z"
      : null,
    version: 1,
    off_resolved: status === "succeeded" ? false : null,
    on_resolved: status === "succeeded" ? true : null,
    queue_position: status === "queued" ? 2 : null,
    ...overrides,
  };
}

export function record(status: TaskRecord["status"], taskId = `task-${status}`): TaskRecord {
  const base = {
    task_id: taskId,
    request,
    created_at: "2026-07-29T01:00:00Z",
    version: 1,
  };
  if (status === "queued") {
    return {
      ...base,
      status,
      phase: null,
      started_at: null,
      finished_at: null,
      failure_category: null,
      failure_phase: null,
      failure_summary: null,
      result: null,
      queue_position: 2,
    };
  }
  if (status === "running") {
    return {
      ...base,
      status,
      phase: "running_off",
      started_at: "2026-07-29T01:01:00Z",
      finished_at: null,
      failure_category: null,
      failure_phase: null,
      failure_summary: null,
      result: null,
      queue_position: null,
    };
  }
  if (status === "succeeded") {
    return {
      ...base,
      status,
      phase: "generating_report",
      started_at: "2026-07-29T01:01:00Z",
      finished_at: "2026-07-29T01:02:00Z",
      failure_category: null,
      failure_phase: null,
      failure_summary: null,
      result: {
        artifact_dir: "/safe/artifacts",
        report_path: "/safe/report.md",
        off_resolved: false,
        on_resolved: true,
      },
      queue_position: null,
    };
  }
  if (status === "cancelled") {
    return {
      ...base,
      status,
      phase: null,
      started_at: null,
      finished_at: "2026-07-29T01:02:00Z",
      failure_category: null,
      failure_phase: null,
      failure_summary: null,
      result: null,
      queue_position: null,
    };
  }
  return {
    ...base,
    status,
    phase: "running_on",
    started_at: "2026-07-29T01:01:00Z",
    finished_at: "2026-07-29T01:02:00Z",
    failure_category: status === "failed" ? "codex_execution_failure" : "worker_interruption",
    failure_phase: "running_on",
    failure_summary: "安全的失败摘要",
    result: null,
    queue_position: null,
  };
}

export function apiStub(overrides: Partial<Record<keyof EvaluationApi, unknown>> = {}): EvaluationApi {
  return {
    getCapabilities: vi.fn().mockResolvedValue(capabilities),
    getHealth: vi.fn().mockResolvedValue(health),
    listTasks: vi.fn().mockResolvedValue([]),
    getTask: vi.fn().mockResolvedValue(record("queued")),
    createTask: vi.fn().mockResolvedValue(record("queued")),
    cancelTask: vi.fn().mockResolvedValue(record("cancelled")),
    getReport: vi.fn().mockResolvedValue(report),
    getRawReport: vi.fn().mockResolvedValue("# report"),
    subscribeTaskEvents: vi.fn().mockReturnValue({ close: vi.fn() }),
    ...overrides,
  } as unknown as EvaluationApi;
}
