import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { TaskEvent } from "../types";
import { TaskDetail } from "./TaskDetail";
import { apiStub, record } from "../test/fixtures";

describe("TaskDetail", () => {
  it("shows immutable parameters, truthful timeline, safe failure, and successful report link", async () => {
    const api = apiStub({ getTask: vi.fn().mockResolvedValue(record("succeeded", "task-done")) });
    const { rerender } = render(<TaskDetail api={api} taskId="task-done" />);
    expect(await screen.findByText("不可变提交参数")).toBeVisible();
    expect(screen.getByText("生成报告")).toBeVisible();
    expect(screen.getByRole("link", { name: "查看验收报告" })).toHaveAttribute("href", "/reports/task-done");

    rerender(
      <TaskDetail
        api={apiStub({ getTask: vi.fn().mockResolvedValue(record("failed", "task-failed")) })}
        taskId="task-failed"
      />,
    );
    expect(await screen.findByText("安全的失败摘要")).toBeVisible();
    expect(screen.getByText("Codex 执行失败")).toBeVisible();
  });

  it("subscribes once, refreshes on SSE, exposes reconnect state, and cleans up on task change", async () => {
    let onEvent!: (event: TaskEvent) => void;
    let onError!: (error: { message: string; reconnecting: boolean; code: "event_stream_disconnected" }) => void;
    const close = vi.fn();
    const subscribeTaskEvents = vi.fn((_id, eventHandler, errorHandler) => {
      onEvent = eventHandler;
      onError = errorHandler;
      return { close };
    });
    const getTask = vi.fn().mockResolvedValue(record("running", "task-a"));
    const api = apiStub({ getTask, subscribeTaskEvents });
    const onTaskChanged = vi.fn();
    const { rerender, unmount } = render(<TaskDetail api={api} taskId="task-a" onTaskChanged={onTaskChanged} />);
    await screen.findByText("OFF 执行");
    expect(subscribeTaskEvents).toHaveBeenCalledTimes(1);
    expect(onTaskChanged).toHaveBeenCalledWith(expect.objectContaining({ task_id: "task-a" }));

    act(() =>
      onError({
        code: "event_stream_disconnected",
        message: "disconnected",
        reconnecting: true,
      }),
    );
    expect(screen.getByText("实时连接中断，正在定时刷新。")).toBeVisible();
    act(() =>
      onEvent({
        task_id: "task-a",
        status: "running",
        phase: "running_on",
        version: 2,
        occurred_at: "2026-07-29T01:02:00Z",
      }),
    );
    await waitFor(() => expect(getTask).toHaveBeenCalledTimes(2));
    expect(onTaskChanged).toHaveBeenCalledTimes(2);

    rerender(<TaskDetail api={api} taskId="task-b" onTaskChanged={onTaskChanged} />);
    expect(close).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(subscribeTaskEvents).toHaveBeenCalledTimes(2));
    unmount();
    expect(close).toHaveBeenCalledTimes(2);
  });
});
