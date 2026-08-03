import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { EvaluationApi } from "../api";
import { ReportIndex } from "./ReportIndex";

function batch(batchId: string, model: string, pauseReason: string) {
  return {
    batch_id: batchId,
    request: {
      powercontext_ref: "latest",
      benchmark: "swebench-pro",
      task_set: "swebench-pro-public-v2",
      model,
      reasoning_effort: "medium",
      treatment_mode: "off_on",
      idempotency_key: `${batchId}-request`,
      usage_pause_percent: 80,
      initial_control_intent: "run",
    },
    total_tasks: 731,
    status: "paused",
    control: {
      intent: "pause",
      usage_pause_percent: 80,
      pause_reason: pauseReason,
      updated_at: "2026-08-03T00:00:00Z",
      version: 1,
    },
    created_at: "2026-08-02T00:00:00Z",
    started_at: "2026-08-02T00:01:00Z",
    finished_at: null,
    resolved_powercontext_sha: "0123456789abcdef0123456789abcdef01234567",
  };
}

describe("ReportIndex", () => {
  it("keeps all batches visible when Luna pauses for an infrastructure failure", async () => {
    const sol = batch("batch-sol", "gpt-5.6-sol", "user");
    const luna = batch("batch-luna", "gpt-5.6-luna", "infrastructure_failure");
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      new Response(JSON.stringify([sol, luna]), { headers: { "Content-Type": "application/json" } }),
    );

    render(<ReportIndex api={new EvaluationApi({ fetch })} navigate={vi.fn()} />);

    expect(await screen.findByText("batch-sol")).toBeVisible();
    expect(screen.getByText("batch-luna")).toBeVisible();
    expect(screen.queryByText("评测批次暂时无法加载。")).not.toBeInTheDocument();
  });
});
