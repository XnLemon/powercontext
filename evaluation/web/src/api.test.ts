import { describe, expect, it, vi } from "vitest";

import { ApiError, EvaluationApi } from "./api";

const validTask = {
  powercontext_ref: "latest",
  benchmark: "swebench-pro",
  instance_id: "instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9",
  model: "gpt-5.6-sol",
  reasoning_effort: "medium",
  treatment_mode: "off_on",
  idempotency_key: "request-1",
} as const;

const queuedTask = {
  task_id: "task-1",
  request: validTask,
  status: "queued",
  phase: null,
  created_at: "2026-07-29T00:00:00Z",
  started_at: null,
  finished_at: null,
  version: 0,
  failure_category: null,
  failure_phase: null,
  failure_summary: null,
  result: null,
  queue_position: 1,
} as const;

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function apiWithResponse(response: Response): {
  api: EvaluationApi;
  fetch: ReturnType<typeof vi.fn>;
} {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(response);
  return { api: new EvaluationApi({ fetch }), fetch };
}

describe("EvaluationApi HTTP", () => {
  it("loads capabilities, health, task summaries, and task detail from relative API URLs", async () => {
    const capabilities = {
      benchmarks: ["swebench-pro"],
      instances: [validTask.instance_id],
      models: ["gpt-5.6-sol"],
      reasoning_efforts: ["medium"],
      treatment_modes: ["off_on"],
    };
    const health = { service: "ok", worker_lease_active: false, queued_tasks: 1, running_tasks: 0 };
    const summary = {
      task_id: "task-1",
      powercontext_ref: "latest",
      instance_id: validTask.instance_id,
      model: "gpt-5.6-sol",
      status: "queued",
      phase: null,
      created_at: "2026-07-29T00:00:00Z",
      started_at: null,
      finished_at: null,
      version: 0,
      off_resolved: null,
      on_resolved: null,
      queue_position: 1,
    };
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(jsonResponse(capabilities))
      .mockResolvedValueOnce(jsonResponse(health))
      .mockResolvedValueOnce(jsonResponse([summary]))
      .mockResolvedValueOnce(jsonResponse(queuedTask));
    const api = new EvaluationApi({ fetch });

    await expect(api.getCapabilities()).resolves.toEqual(capabilities);
    await expect(api.getHealth()).resolves.toEqual(health);
    await expect(api.listTasks({ status: "queued", limit: 25, offset: 0 })).resolves.toEqual([summary]);
    await expect(api.getTask("task-1")).resolves.toEqual(queuedTask);

    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "/api/capabilities",
      "/api/health",
      "/api/tasks?status=queued&limit=25&offset=0",
      "/api/tasks/task-1",
    ]);
  });

  it.each([201, 200])("accepts create status %i and returns the queued task", async (status) => {
    const { api, fetch } = apiWithResponse(jsonResponse(queuedTask, status));

    await expect(api.createTask(validTask)).resolves.toEqual(queuedTask);
    const [url, init] = fetch.mock.calls[0] ?? [];
    expect(url).toBe("/api/tasks");
    expect(init).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(validTask),
      }),
    );
    const headers = new Headers(init?.headers);
    expect(headers.get("Accept")).toBe("application/json");
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("cancels a task with a relative URL and forwards its abort signal", async () => {
    const cancelled = {
      ...queuedTask,
      status: "cancelled",
      finished_at: "2026-07-29T00:01:00Z",
      queue_position: null,
      version: 1,
    };
    const { api, fetch } = apiWithResponse(jsonResponse(cancelled));
    const controller = new AbortController();

    await expect(api.cancelTask("task-1", controller.signal)).resolves.toEqual(cancelled);
    expect(fetch).toHaveBeenCalledWith(
      "/api/tasks/task-1/cancel",
      expect.objectContaining({ method: "POST", signal: controller.signal }),
    );
  });

  it("loads the exact structured report contract", async () => {
    const evidence = {
      mcp_requests: 1,
      prompt_sources: 2,
      plugin_checkout_sha: "abc",
      plugin_id: "powercontext",
      plugin_installed: true,
      plugin_version: "0.1.0",
      scope_id: "scope",
      server_ready: true,
    };
    const report = {
      task_id: "task-1",
      acceptance_valid: true,
      off: {
        arm: "off",
        resolution: "unresolved",
        input_tokens: 10,
        output_tokens: 20,
        elapsed_seconds: 1.5,
        patch_bytes: 0,
      },
      on: {
        arm: "on",
        resolution: "resolved",
        input_tokens: 8,
        output_tokens: 12,
        elapsed_seconds: 1,
        patch_bytes: 100,
      },
      comparison: {
        input_tokens: { off: 10, on: 8, delta: -2, percent: -20 },
        output_tokens: { off: 20, on: 12, delta: -8, percent: -40 },
        elapsed_seconds: { off: 1.5, on: 1, delta: -0.5, percent: -33.333 },
        patch_bytes: { off: 0, on: 100, delta: 100, percent: null },
      },
      evidence: { off: evidence, on: evidence },
      revisions: { powercontext: "abc" },
      configuration: { model: "gpt-5.6-sol" },
      generated_at: "2026-07-29T00:02:00Z",
    };
    const { api } = apiWithResponse(jsonResponse(report));

    await expect(api.getReport("task-1")).resolves.toEqual(report);
  });

  it("loads raw report markdown only from a text response", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      new Response("# Report", { headers: { "Content-Type": "text/plain; charset=utf-8" } }),
    );

    await expect(new EvaluationApi({ fetch }).getRawReport("task-1")).resolves.toBe("# Report");
    expect(fetch).toHaveBeenCalledWith(
      "/api/tasks/task-1/report.md",
      expect.objectContaining({ headers: { Accept: "text/plain" } }),
    );
  });

  it("turns the fixed JSON error envelope into ApiError", async () => {
    const { api } = apiWithResponse(
      jsonResponse({ error: { code: "task_not_found", message: "The requested task does not exist." } }, 404),
    );

    await expect(api.getTask("missing")).rejects.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError",
        status: 404,
        code: "task_not_found",
        message: "The requested task does not exist.",
      }),
    );
  });

  it.each([
    new Response("<secret>upstream failed</secret>", {
      status: 502,
      headers: { "Content-Type": "text/html" },
    }),
    new Response("{broken", { status: 500, headers: { "Content-Type": "application/json" } }),
  ])("uses a safe generic message for non-JSON or malformed errors", async (response) => {
    const { api } = apiWithResponse(response);

    await expect(api.getHealth()).rejects.toMatchObject({
      name: "ApiError",
      code: "request_failed",
      message: "The evaluation service could not complete the request.",
    });
    await expect(api.getHealth()).rejects.not.toThrow(/secret|upstream|broken/i);
  });

  it("uses a safe generic abort error without leaking the thrown value", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockRejectedValue(new DOMException("private reason", "AbortError"));

    await expect(new EvaluationApi({ fetch }).getHealth()).rejects.toMatchObject({
      name: "ApiError",
      code: "request_aborted",
      message: "The evaluation request was cancelled.",
    });
  });
});

type Listener = (event: Event) => void;

class FakeEventSource {
  readonly url: string;
  readonly listeners = new Map<string, Set<Listener>>();
  close = vi.fn();

  constructor(url: string) {
    this.url = url;
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    const callback: Listener =
      typeof listener === "function" ? listener : (event) => listener.handleEvent(event);
    const listeners = this.listeners.get(type) ?? new Set<Listener>();
    listeners.add(callback);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    if (typeof listener === "function") {
      this.listeners.get(type)?.delete(listener);
    }
  }

  emit(type: string, data?: string): void {
    const event = data === undefined ? new Event(type) : new MessageEvent(type, { data });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

describe("EvaluationApi task events", () => {
  it("parses task events, ignores heartbeat, and closes on terminal state", () => {
    let source: FakeEventSource | undefined;
    const events = vi.fn();
    const errors = vi.fn();
    const api = new EvaluationApi({
      eventSourceFactory: (url) => {
        source = new FakeEventSource(url);
        return source;
      },
    });
    const subscription = api.subscribeTaskEvents("task-1", events, errors);

    expect(source?.url).toBe("/api/tasks/task-1/events");
    source?.emit("heartbeat");
    expect(events).not.toHaveBeenCalled();
    source?.emit(
      "task",
      JSON.stringify({
        task_id: "task-1",
        status: "running",
        phase: "running_off",
        version: 1,
        occurred_at: "2026-07-29T00:01:00Z",
      }),
    );
    source?.emit(
      "task",
      JSON.stringify({
        task_id: "task-1",
        status: "succeeded",
        phase: "generating_report",
        version: 2,
        occurred_at: "2026-07-29T00:02:00Z",
      }),
    );
    source?.emit("error");

    expect(events).toHaveBeenCalledTimes(2);
    expect(source?.close).toHaveBeenCalledTimes(1);
    expect(errors).not.toHaveBeenCalled();
    subscription.close();
    expect(source?.close).toHaveBeenCalledTimes(1);
  });

  it("reports native reconnect for nonterminal errors and registers no duplicate listeners", () => {
    let source: FakeEventSource | undefined;
    const errors = vi.fn();
    const api = new EvaluationApi({
      eventSourceFactory: (url) => {
        source = new FakeEventSource(url);
        return source;
      },
    });

    const subscription = api.subscribeTaskEvents("task-1", vi.fn(), errors);
    source?.emit("error");
    source?.emit("error");

    expect(errors).toHaveBeenCalledTimes(2);
    expect(errors).toHaveBeenLastCalledWith({
      code: "event_stream_disconnected",
      message: "Live updates were interrupted. Reconnecting automatically.",
      reconnecting: true,
    });
    expect(source?.listeners.get("task")?.size).toBe(1);
    expect(source?.listeners.get("error")?.size).toBe(1);
    expect(source?.close).not.toHaveBeenCalled();
    subscription.close();
    subscription.close();
    expect(source?.close).toHaveBeenCalledTimes(1);
  });
});
