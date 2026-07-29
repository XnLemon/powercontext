import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { apiStub, batchRecord } from "./test/fixtures";

describe("App batch report navigation", () => {
  beforeEach(() => window.history.replaceState({}, "", "/"));

  it("shows only the two report destinations and a complete-batch launcher", async () => {
    render(<App api={apiStub()} />);

    expect(screen.getByRole("heading", { name: "总体报告" })).toBeVisible();
    expect(screen.getByRole("navigation", { name: "报告导航" })).toBeVisible();
    expect(screen.getByRole("link", { name: "总体报告" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "任务详细报告" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getAllByRole("navigation")).toHaveLength(1);
    expect(screen.getAllByRole("link")).toHaveLength(3);
    expect(screen.queryByRole("link", { name: /工作台|测试任务|验收报告|单任务详情/ })).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "预览评测" })).toBeVisible();
    expect(await screen.findByText("Worker 工作中")).toBeVisible();
  });

  it("describes an inactive worker lease as idle instead of disconnected", async () => {
    const api = apiStub({
      getHealth: vi.fn().mockResolvedValue({
        service: "ok",
        worker_lease_active: false,
        queued_tasks: 0,
        running_tasks: 0,
      }),
    });

    render(<App api={api} />);

    expect(await screen.findByText("Worker 空闲")).toBeVisible();
    expect(screen.queryByText("Worker 未连接")).not.toBeInTheDocument();
  });

  it("navigates a newly created batch to its aggregate report", async () => {
    const created = batchRecord({ batch_id: "batch/new" });
    const api = apiStub({ createBatch: vi.fn().mockResolvedValue(created) });
    render(<App api={api} />);

    fireEvent.click(await screen.findByRole("button", { name: "预览评测" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认并开始评测" }));

    await waitFor(() => expect(window.location.pathname).toBe("/report/batch%2Fnew"));
    expect(await screen.findByRole("heading", { name: "总体报告" })).toBeVisible();
    expect(screen.getByRole("link", { name: "任务详细报告" })).not.toHaveAttribute("aria-disabled");
  });

  it("keeps the task-report destination active on a contextual task detail route", async () => {
    window.history.replaceState({}, "", "/report/batch-one/tasks/task-one");
    render(<App api={apiStub()} />);

    expect(await screen.findByRole("heading", { name: "单任务详情" })).toBeVisible();
    expect(screen.getByRole("link", { name: "任务详细报告" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("link", { name: "单任务详情" })).not.toBeInTheDocument();
  });

  it("never invents a task detail when no concrete task was selected", async () => {
    window.history.replaceState({}, "", "/report/batch-one/tasks");
    render(<App api={apiStub()} />);

    expect(await screen.findByRole("heading", { name: "任务详细报告" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "单任务详情" })).not.toBeInTheDocument();
  });
});
