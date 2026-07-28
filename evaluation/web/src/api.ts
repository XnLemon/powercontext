import type {
  Capabilities,
  EventStreamError,
  HealthResponse,
  ReportResponse,
  TaskCreate,
  TaskEvent,
  TaskEventSubscription,
  TaskListOptions,
  TaskRecord,
  TaskStatus,
  TaskSummary,
} from "./types";
import { z } from "zod";

const TASK_STATUSES = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "interrupted",
  "cancelled",
] as const;
const TERMINAL_STATUSES = new Set<TaskStatus>(["succeeded", "failed", "interrupted", "cancelled"]);
const GENERIC_ERROR_MESSAGE = "The evaluation service could not complete the request.";
const INSTANCE_ID = "instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9" as const;

type Fetch = typeof globalThis.fetch;

interface EventSourceLike {
  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void;
  removeEventListener(type: string, listener: EventListenerOrEventListenerObject): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

export interface EvaluationApiOptions {
  fetch?: Fetch;
  eventSourceFactory?: EventSourceFactory;
}

export class ApiError extends Error {
  readonly status: number | null;
  readonly code: string;

  constructor(status: number | null, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

const timestampSchema = z.iso
  .datetime({ offset: true })
  .refine((value) => value.endsWith("Z") || value.endsWith("+00:00"), "Timestamp must use UTC.");
const nonnegativeIntegerSchema = z.number().int().nonnegative();
const queuePositionSchema = z.number().int().positive().nullable();
const nonnegativeNumberSchema = z.number().nonnegative();
const taskStatusSchema = z.enum(TASK_STATUSES);
const taskPhaseSchema = z.enum([
  "preparing",
  "validating_gold",
  "running_off",
  "running_on",
  "official_evaluation",
  "generating_report",
]);
const failureCategorySchema = z.enum([
  "invalid_request",
  "queue_unavailable",
  "source_resolution_failure",
  "environment_preparation_failure",
  "gold_validation_failure",
  "codex_execution_failure",
  "treatment_validation_failure",
  "official_evaluator_failure",
  "report_generation_failure",
  "worker_interruption",
  "internal",
]);

const taskCreateSchema = z.strictObject({
  powercontext_ref: z.union([z.literal("latest"), z.string().regex(/^commit:[0-9a-fA-F]{40}$/)]),
  benchmark: z.literal("swebench-pro"),
  instance_id: z.literal(INSTANCE_ID),
  model: z.literal("gpt-5.6-sol"),
  reasoning_effort: z.literal("medium"),
  treatment_mode: z.literal("off_on"),
  idempotency_key: z.string().min(8).max(128).regex(/^[A-Za-z0-9._-]+$/),
});

const taskResultSchema = z.strictObject({
  artifact_dir: z.string(),
  report_path: z.string(),
  off_resolved: z.boolean(),
  on_resolved: z.boolean(),
});

const taskRecordBaseShape = {
  task_id: z.string(),
  request: taskCreateSchema,
  created_at: timestampSchema,
  version: nonnegativeIntegerSchema,
  queue_position: queuePositionSchema,
};
const noFailureShape = {
  failure_category: z.null(),
  failure_phase: z.null(),
  failure_summary: z.null(),
};
const taskRecordSchema = z
  .discriminatedUnion("status", [
    z.strictObject({
      ...taskRecordBaseShape,
      status: z.literal("queued"),
      phase: z.null(),
      started_at: z.null(),
      finished_at: z.null(),
      ...noFailureShape,
      result: z.null(),
    }),
    z.strictObject({
      ...taskRecordBaseShape,
      status: z.literal("running"),
      phase: taskPhaseSchema.nullable(),
      started_at: timestampSchema,
      finished_at: z.null(),
      ...noFailureShape,
      result: z.null(),
    }),
    z.strictObject({
      ...taskRecordBaseShape,
      status: z.literal("succeeded"),
      phase: taskPhaseSchema.nullable(),
      started_at: timestampSchema,
      finished_at: timestampSchema,
      ...noFailureShape,
      result: taskResultSchema,
    }),
    z.strictObject({
      ...taskRecordBaseShape,
      status: z.enum(["failed", "interrupted"]),
      phase: taskPhaseSchema.nullable(),
      started_at: timestampSchema,
      finished_at: timestampSchema,
      failure_category: failureCategorySchema,
      failure_phase: taskPhaseSchema.nullable(),
      failure_summary: z.string().min(1).max(500),
      result: z.null(),
    }),
    z.strictObject({
      ...taskRecordBaseShape,
      status: z.literal("cancelled"),
      phase: z.null(),
      started_at: z.null(),
      finished_at: timestampSchema,
      ...noFailureShape,
      result: z.null(),
    }),
  ])
  .superRefine((record, context) => {
    const created = Date.parse(record.created_at);
    const started = record.started_at === null ? null : Date.parse(record.started_at);
    const finished = record.finished_at === null ? null : Date.parse(record.finished_at);
    if (started !== null && started < created) {
      context.addIssue({ code: "custom", message: "Task start precedes creation." });
    }
    if (finished !== null && finished < (started ?? created)) {
      context.addIssue({ code: "custom", message: "Task finish precedes its prior lifecycle timestamp." });
    }
  });

const taskSummarySchema = z.strictObject({
  task_id: z.string(),
  powercontext_ref: z.string(),
  instance_id: z.string(),
  model: z.string(),
  status: taskStatusSchema,
  phase: taskPhaseSchema.nullable(),
  created_at: timestampSchema,
  started_at: timestampSchema.nullable(),
  finished_at: timestampSchema.nullable(),
  version: nonnegativeIntegerSchema,
  off_resolved: z.boolean().nullable(),
  on_resolved: z.boolean().nullable(),
  queue_position: queuePositionSchema,
});

const taskEventSchema = z.strictObject({
  task_id: z.string(),
  status: taskStatusSchema,
  phase: taskPhaseSchema.nullable(),
  version: nonnegativeIntegerSchema,
  occurred_at: timestampSchema,
});

const capabilitiesSchema = z.strictObject({
  benchmarks: z.array(z.literal("swebench-pro")),
  instances: z.array(z.literal(INSTANCE_ID)),
  models: z.array(z.literal("gpt-5.6-sol")),
  reasoning_efforts: z.array(z.literal("medium")),
  treatment_modes: z.array(z.literal("off_on")),
});

const healthSchema = z.strictObject({
  service: z.literal("ok"),
  worker_lease_active: z.boolean(),
  queued_tasks: nonnegativeIntegerSchema,
  running_tasks: nonnegativeIntegerSchema,
});

function armSchema<Arm extends "off" | "on">(arm: Arm) {
  return z.strictObject({
    arm: z.literal(arm),
    resolution: z.enum(["resolved", "unresolved"]),
    input_tokens: nonnegativeIntegerSchema.nullable(),
    output_tokens: nonnegativeIntegerSchema.nullable(),
    elapsed_seconds: nonnegativeNumberSchema.nullable(),
    patch_bytes: nonnegativeIntegerSchema.nullable(),
  });
}

const metricComparisonSchema = z.strictObject({
  off: nonnegativeNumberSchema,
  on: nonnegativeNumberSchema,
  delta: z.number(),
  percent: z.number().nullable(),
});
const comparisonSchema = z.strictObject({
  input_tokens: metricComparisonSchema.nullable(),
  output_tokens: metricComparisonSchema.nullable(),
  elapsed_seconds: metricComparisonSchema.nullable(),
  patch_bytes: metricComparisonSchema.nullable(),
});
const treatmentEvidenceSchema = z.strictObject({
  mcp_requests: nonnegativeIntegerSchema,
  prompt_sources: nonnegativeIntegerSchema,
  plugin_checkout_sha: z.string(),
  plugin_id: z.string(),
  plugin_installed: z.boolean(),
  plugin_version: z.string(),
  scope_id: z.string(),
  server_ready: z.boolean(),
});
const reportSchema = z.strictObject({
  task_id: z.string(),
  acceptance_valid: z.boolean(),
  off: armSchema("off"),
  on: armSchema("on"),
  comparison: comparisonSchema,
  evidence: z.strictObject({
    off: treatmentEvidenceSchema,
    on: treatmentEvidenceSchema,
  }),
  revisions: z.record(z.string(), z.string()),
  configuration: z.record(z.string(), z.string()),
  generated_at: timestampSchema,
});
const errorEnvelopeSchema = z.strictObject({
  error: z.strictObject({
    code: z.string(),
    message: z.string(),
  }),
});

function validateWithSchema<T>(schema: z.ZodType<T>, value: unknown): T {
  return schema.parse(value);
}

function validateTaskRecord(value: unknown): TaskRecord {
  return validateWithSchema(taskRecordSchema, value);
}

function validateTaskSummary(value: unknown): TaskSummary {
  return validateWithSchema(taskSummarySchema, value);
}

function validateTaskEvent(value: unknown): TaskEvent {
  return validateWithSchema(taskEventSchema, value);
}

function validateCapabilities(value: unknown): Capabilities {
  return validateWithSchema(capabilitiesSchema, value);
}

function validateHealth(value: unknown): HealthResponse {
  return validateWithSchema(healthSchema, value);
}

function validateReport(value: unknown): ReportResponse {
  return validateWithSchema(reportSchema, value);
}

function mediaType(response: Response): string {
  return response.headers.get("Content-Type")?.split(";", 1)[0]?.trim().toLowerCase() ?? "";
}

function apiPath(path: string): string {
  return `/api${path}`;
}

function taskPath(taskId: string, suffix = ""): string {
  return apiPath(`/tasks/${encodeURIComponent(taskId)}${suffix}`);
}

function withSignal(signal: AbortSignal | undefined): Pick<RequestInit, "signal"> {
  return signal === undefined ? {} : { signal };
}

export class EvaluationApi {
  readonly #fetch: Fetch;
  readonly #eventSourceFactory: EventSourceFactory;

  constructor(options: EvaluationApiOptions = {}) {
    this.#fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.#eventSourceFactory =
      options.eventSourceFactory ?? ((url) => new globalThis.EventSource(url));
  }

  getCapabilities(signal?: AbortSignal): Promise<Capabilities> {
    return this.#json(apiPath("/capabilities"), validateCapabilities, withSignal(signal));
  }

  getHealth(signal?: AbortSignal): Promise<HealthResponse> {
    return this.#json(apiPath("/health"), validateHealth, withSignal(signal));
  }

  listTasks(options: TaskListOptions = {}, signal?: AbortSignal): Promise<TaskSummary[]> {
    const query = new URLSearchParams();
    if (options.status !== undefined) query.set("status", options.status);
    if (options.limit !== undefined) query.set("limit", String(options.limit));
    if (options.offset !== undefined) query.set("offset", String(options.offset));
    const suffix = query.size === 0 ? "" : `?${query.toString()}`;
    return this.#json(
      `${apiPath("/tasks")}${suffix}`,
      (value) => {
        if (!Array.isArray(value)) throw new ApiError(null, "invalid_response", GENERIC_ERROR_MESSAGE);
        return value.map(validateTaskSummary);
      },
      withSignal(signal),
    );
  }

  getTask(taskId: string, signal?: AbortSignal): Promise<TaskRecord> {
    return this.#json(taskPath(taskId), validateTaskRecord, withSignal(signal));
  }

  createTask(task: TaskCreate, signal?: AbortSignal): Promise<TaskRecord> {
    return this.#json(apiPath("/tasks"), validateTaskRecord, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(task),
      ...withSignal(signal),
    });
  }

  cancelTask(taskId: string, signal?: AbortSignal): Promise<TaskRecord> {
    return this.#json(taskPath(taskId, "/cancel"), validateTaskRecord, {
      method: "POST",
      ...withSignal(signal),
    });
  }

  getReport(taskId: string, signal?: AbortSignal): Promise<ReportResponse> {
    return this.#json(taskPath(taskId, "/report"), validateReport, withSignal(signal));
  }

  async getRawReport(taskId: string, signal?: AbortSignal): Promise<string> {
    const response = await this.#request(taskPath(taskId, "/report.md"), {
      headers: { Accept: "text/plain" },
      ...withSignal(signal),
    });
    if (mediaType(response) !== "text/plain") {
      throw new ApiError(response.status, "invalid_response", GENERIC_ERROR_MESSAGE);
    }
    return response.text();
  }

  subscribeTaskEvents(
    taskId: string,
    onEvent: (event: TaskEvent) => void,
    onError: (error: EventStreamError) => void = () => undefined,
  ): TaskEventSubscription {
    const source = this.#eventSourceFactory(taskPath(taskId, "/events"));
    let closed = false;

    const close = (): void => {
      if (closed) return;
      closed = true;
      source.removeEventListener("task", taskListener);
      source.removeEventListener("error", errorListener);
      source.close();
    };
    const taskListener: EventListener = (nativeEvent) => {
      if (closed || !(nativeEvent instanceof MessageEvent) || typeof nativeEvent.data !== "string") return;
      try {
        const event = validateTaskEvent(JSON.parse(nativeEvent.data) as unknown);
        onEvent(event);
        if (TERMINAL_STATUSES.has(event.status)) close();
      } catch {
        onError({
          code: "invalid_event",
          message: "A live update could not be read safely.",
          reconnecting: true,
        });
      }
    };
    const errorListener: EventListener = () => {
      if (closed) return;
      onError({
        code: "event_stream_disconnected",
        message: "Live updates were interrupted. Reconnecting automatically.",
        reconnecting: true,
      });
    };

    source.addEventListener("task", taskListener);
    source.addEventListener("error", errorListener);
    return { close };
  }

  async #json<T>(
    url: string,
    validate: (value: unknown) => T,
    init: RequestInit,
  ): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    const response = await this.#request(url, { ...init, headers });
    if (mediaType(response) !== "application/json") {
      throw new ApiError(response.status, "invalid_response", GENERIC_ERROR_MESSAGE);
    }
    try {
      return validate((await response.json()) as unknown);
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(response.status, "invalid_response", GENERIC_ERROR_MESSAGE);
    }
  }

  async #request(url: string, init: RequestInit): Promise<Response> {
    let response: Response;
    try {
      response = await this.#fetch(url, init);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new ApiError(null, "request_aborted", "The evaluation request was cancelled.");
      }
      throw new ApiError(null, "request_failed", GENERIC_ERROR_MESSAGE);
    }
    if (response.ok) return response;

    if (mediaType(response) === "application/json") {
      try {
        const parsed = errorEnvelopeSchema.safeParse((await response.json()) as unknown);
        if (parsed.success) {
          throw new ApiError(response.status, parsed.data.error.code, parsed.data.error.message);
        }
      } catch (error) {
        if (error instanceof ApiError) throw error;
      }
    }
    throw new ApiError(response.status, "request_failed", GENERIC_ERROR_MESSAGE);
  }
}
