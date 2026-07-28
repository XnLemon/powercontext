import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { apiStub, deferred, record, summary } from "./test/fixtures";

describe("App", () => {
  beforeEach(() => window.history.replaceState({}, "", "/"));

  it("renders semantic shell, safe environment health, and workbench priority", async () => {
    const api = apiStub({
      listTasks: vi.fn().mockResolvedValue([summary("running", "task-live")]),
      getTask: vi.fn().mockResolvedValue(record("running", "task-live")),
    });
    render(<App api={api} />);

    expect(screen.getByRole("heading", { name: "评测工作台" })).toBeVisible();
    expect(screen.getByRole("navigation", { name: "主导航" })).toBeVisible();
    expect(screen.getByRole("link", { name: "工作台" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText("m0")).toBeVisible();
    expect(screen.getByText("服务正常")).toBeVisible();
    expect(screen.getByText("Worker 在线")).toBeVisible();
    expect(await screen.findByRole("heading", { name: "当前运行任务" })).toBeVisible();
    expect(screen.queryByText(/用户|登录|配置编辑/i)).not.toBeInTheDocument();
  });

  it("uses History routes and supports back navigation", async () => {
    render(<App api={apiStub()} />);
    fireEvent.click(screen.getByRole("link", { name: "测试任务" }));
    expect(await screen.findByRole("heading", { name: "测试任务" })).toBeVisible();
    expect(screen.getByRole("link", { name: "测试任务" })).toHaveAttribute("aria-current", "page");

    actPop("/");
    expect(await screen.findByRole("heading", { name: "评测工作台" })).toBeVisible();
  });

  it("shows latest succeeded report boundary only when no task is running", async () => {
    render(<App api={apiStub({ listTasks: vi.fn().mockResolvedValue([summary("succeeded", "task-latest")]) })} />);
    expect(await screen.findByRole("heading", { name: "最近完成" })).toBeVisible();
    expect(screen.getByRole("link", { name: "查看验收报告" })).toHaveAttribute("href", "/reports/task-latest");
    expect(screen.queryByText(/通过|未通过/)).not.toBeInTheDocument();
  });

  it("routes task details and the report index without preloading conclusions", async () => {
    window.history.replaceState({}, "", "/tasks/task-one");
    render(<App api={apiStub({ getTask: vi.fn().mockResolvedValue(record("queued", "task-one")) })} />);
    expect(await screen.findByRole("heading", { name: "任务详情" })).toBeVisible();

    fireEvent.click(screen.getByRole("link", { name: "验收报告" }));
    expect(await screen.findByRole("heading", { name: "验收报告" })).toBeVisible();
    expect(screen.getByText("暂无已完成的验收报告。")).toBeVisible();
  });

  it("loads a report route and exposes succeeded reports from the report index", async () => {
    const api = apiStub({
      listTasks: vi.fn().mockResolvedValue([summary("succeeded", "task/report")]),
    });
    window.history.replaceState({}, "", "/reports/task%2Freport");
    render(<App api={api} />);
    expect(await screen.findByText("验收有效")).toBeVisible();
    expect(api.getReport).toHaveBeenCalledWith("task/report", expect.any(AbortSignal));

    actPop("/reports");
    expect(await screen.findByRole("link", { name: "查看 task/report 的验收报告" })).toHaveAttribute(
      "href",
      "/reports/task%2Freport",
    );
  });

  it("ignores a stale workbench overview response after its API changes", async () => {
    const stale = deferred<ReturnType<typeof summary>[]>();
    const fresh = deferred<ReturnType<typeof summary>[]>();
    const firstApi = apiStub({ listTasks: vi.fn().mockReturnValue(stale.promise) });
    const secondList = vi.fn().mockReturnValue(fresh.promise);
    const { rerender } = render(<App api={firstApi} />);
    rerender(<App api={apiStub({ listTasks: secondList })} />);
    fresh.resolve([summary("succeeded", "task-fresh")]);
    expect(await screen.findByText("task-fresh")).toBeVisible();
    stale.resolve([summary("running", "task-stale")]);
    await waitFor(() => expect(screen.queryByText("task-stale")).not.toBeInTheDocument());
    expect(secondList.mock.calls[0]?.[1]).toBeInstanceOf(AbortSignal);
  });
});

function actPop(path: string): void {
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
