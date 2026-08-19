import type { PowerContextConfig } from "./config.js";

export class PowerContextRequestError extends Error {
  readonly status?: number;
  readonly path: string;

  constructor(path: string, message: string, status?: number) {
    super(message);
    this.name = "PowerContextRequestError";
    this.path = path;
    this.status = status;
  }
}

export type PowerContextClient = ReturnType<typeof createPowerContextClient>;

export function createPowerContextClient(
  getConfig: () => PowerContextConfig,
  log: (message: string) => void,
) {
  async function request<T>(
    method: "GET" | "POST",
    path: string,
    body?: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<T> {
    const config = getConfig();
    if (!config.endpoint) {
      throw new PowerContextRequestError(path, "PowerContext endpoint is not configured");
    }
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) {
      controller.abort();
    }
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, config.timeoutMs);
    try {
      const token = process.env[config.tokenEnv];
      const headers: Record<string, string> = { "content-type": "application/json" };
      if (token) {
        headers.authorization = `Bearer ${token}`;
      }
      let response: Response;
      try {
        response = await fetch(`${config.endpoint}${path}`, {
          method,
          headers,
          ...(body ? { body: JSON.stringify(body) } : {}),
          signal: controller.signal,
        });
      } catch (error) {
        const reason = timedOut
          ? `request timed out after ${config.timeoutMs}ms`
          : signal?.aborted
            ? "request aborted"
            : String(error);
        throw new PowerContextRequestError(path, reason);
      }
      const raw = await response.text();
      let payload: unknown = {};
      if (raw.trim()) {
        try {
          payload = JSON.parse(raw);
        } catch {
          payload = { raw };
        }
      }
      if (!response.ok) {
        const record = typeof payload === "object" && payload !== null ? payload : undefined;
        const error =
          record && "error" in record && typeof record.error === "object" && record.error !== null
            ? record.error
            : undefined;
        const detail =
          error && "message" in error && typeof error.message === "string"
            ? error.message
            : record && "detail" in record && typeof record.detail === "string"
              ? record.detail
              : `HTTP ${response.status}`;
        throw new PowerContextRequestError(path, detail, response.status);
      }
      return payload as T;
    } catch (error) {
      if (error instanceof PowerContextRequestError) {
        throw error;
      }
      log(`PowerContext request failed for ${path}: ${String(error)}`);
      throw new PowerContextRequestError(path, String(error));
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }

  return {
    get<T>(path: string, signal?: AbortSignal) {
      return request<T>("GET", path, undefined, signal);
    },
    post<T>(path: string, body: Record<string, unknown>, signal?: AbortSignal) {
      return request<T>("POST", path, body, signal);
    },
  };
}
