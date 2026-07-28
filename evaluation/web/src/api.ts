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

const TASK_STATUSES = new Set<TaskStatus>([
  "queued",
  "running",
  "succeeded",
  "failed",
  "interrupted",
  "cancelled",
]);
const TERMINAL_STATUSES = new Set<TaskStatus>(["succeeded", "failed", "interrupted", "cancelled"]);
const GENERIC_ERROR_MESSAGE = "The evaluation service could not complete the request.";

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

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isTaskStatus(value: unknown): value is TaskStatus {
  return typeof value === "string" && TASK_STATUSES.has(value as TaskStatus);
}

function requireObject(value: unknown): Record<string, unknown> {
  if (!isObject(value)) throw new ApiError(null, "invalid_response", GENERIC_ERROR_MESSAGE);
  return value;
}

function validateTaskRecord(value: unknown): TaskRecord {
  const object = requireObject(value);
  if (
    typeof object.task_id !== "string" ||
    !isTaskStatus(object.status) ||
    typeof object.version !== "number" ||
    !isObject(object.request) ||
    !("queue_position" in object)
  ) {
    throw new ApiError(null, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return value as TaskRecord;
}

function validateTaskSummary(value: unknown): TaskSummary {
  const object = requireObject(value);
  if (
    typeof object.task_id !== "string" ||
    !isTaskStatus(object.status) ||
    typeof object.version !== "number" ||
    !("queue_position" in object)
  ) {
    throw new ApiError(null, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return value as TaskSummary;
}

function validateTaskEvent(value: unknown): TaskEvent {
  const object = requireObject(value);
  if (
    typeof object.task_id !== "string" ||
    !isTaskStatus(object.status) ||
    typeof object.version !== "number" ||
    typeof object.occurred_at !== "string" ||
    !("phase" in object)
  ) {
    throw new ApiError(null, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return value as TaskEvent;
}

function validateCapabilities(value: unknown): Capabilities {
  const object = requireObject(value);
  const keys = ["benchmarks", "instances", "models", "reasoning_efforts", "treatment_modes"] as const;
  if (!keys.every((key) => Array.isArray(object[key]))) {
    throw new ApiError(null, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return value as Capabilities;
}

function validateHealth(value: unknown): HealthResponse {
  const object = requireObject(value);
  if (
    object.service !== "ok" ||
    typeof object.worker_lease_active !== "boolean" ||
    typeof object.queued_tasks !== "number" ||
    typeof object.running_tasks !== "number"
  ) {
    throw new ApiError(null, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return value as HealthResponse;
}

function validateReport(value: unknown): ReportResponse {
  const object = requireObject(value);
  if (
    typeof object.task_id !== "string" ||
    typeof object.acceptance_valid !== "boolean" ||
    !isObject(object.off) ||
    object.off.arm !== "off" ||
    !isObject(object.on) ||
    object.on.arm !== "on" ||
    !isObject(object.comparison) ||
    !isObject(object.evidence)
  ) {
    throw new ApiError(null, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return value as ReportResponse;
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
        const payload = requireObject((await response.json()) as unknown);
        const envelope = requireObject(payload.error);
        if (typeof envelope.code === "string" && typeof envelope.message === "string") {
          throw new ApiError(response.status, envelope.code, envelope.message);
        }
      } catch (error) {
        if (error instanceof ApiError && error.status === response.status && error.code !== "invalid_response") {
          throw error;
        }
      }
    }
    throw new ApiError(response.status, "request_failed", GENERIC_ERROR_MESSAGE);
  }
}
