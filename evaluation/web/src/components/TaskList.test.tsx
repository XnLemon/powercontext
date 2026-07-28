import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TaskList } from "./TaskList";
import { apiStub, record, summary } from "../test/fixtures";

describe("TaskList", () => {
  it("shows truthful distinct statuses, fields, running emphasis, queue position, and no percent", async () => {
    const tasks = ["queued", "running", "succeeded", "failed", "interrupted", "cancelled"].map((status) =>
      summary(status as ReturnType<typeof summary>["status"]),
    );
    render(<TaskList api={apiStub({ listTasks: vi.fn().mockResolvedValue(tasks) })} onSelect={() => undefined} />);

    expect(await screen.findByText("排队中")).toBeVisible();
    for (const label of ["运行中", "已完成", "失败", "已中断", "已取消"]) {
      expect(screen.getAllByText(label).some((element) => element.matches(".status"))).toBe(true);
    }
    expect(screen.getByText("队列第 2 位")).toBeVisible();
    expect(screen.getByText("OFF 执行")).toBeVisible();
    expect(screen.getByRole("row", { name: /task-running/ })).toHaveClass("task-row--running");
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("applies status filter and cancels only queued tasks after confirmation then refreshes", async () => {
    const listTasks = vi
      .fn()
      .mockResolvedValueOnce([summary("queued")])
      .mockResolvedValueOnce([summary("cancelled")])
      .mockResolvedValueOnce([]);
    const cancelTask = vi.fn().mockResolvedValue(record("cancelled"));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<TaskList api={apiStub({ listTasks, cancelTask })} onSelect={() => undefined} />);

    await screen.findByText("排队中");
    fireEvent.click(screen.getByRole("button", { name: "取消 task-queued" }));
    await waitFor(() => expect(cancelTask).toHaveBeenCalledWith("task-queued"));
    await waitFor(() => expect(screen.getAllByText("已取消").some((element) => element.matches(".status"))).toBe(true));

    fireEvent.change(screen.getByLabelText("状态筛选"), { target: { value: "running" } });
    await waitFor(() => expect(listTasks).toHaveBeenLastCalledWith({ limit: 50, offset: 0, status: "running" }));
    expect(screen.getByText("没有符合条件的任务。")).toBeVisible();
  });

  it("offers retry after a safe loading error", async () => {
    const listTasks = vi.fn().mockRejectedValueOnce(new Error("secret")).mockResolvedValueOnce([]);
    render(<TaskList api={apiStub({ listTasks })} onSelect={() => undefined} />);
    expect(await screen.findByText("任务列表暂时无法加载。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("还没有测试任务。")).toBeVisible();
  });

  it("keeps a queued row actionable after cancellation rejection and succeeds on retry", async () => {
    const user = userEvent.setup();
    const listTasks = vi
      .fn()
      .mockResolvedValueOnce([summary("queued")])
      .mockResolvedValueOnce([summary("cancelled")]);
    const cancelTask = vi
      .fn()
      .mockRejectedValueOnce(new Error("<raw>private upstream</raw>"))
      .mockResolvedValueOnce(record("cancelled"));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<TaskList api={apiStub({ listTasks, cancelTask })} onSelect={() => undefined} />);

    const cancel = await screen.findByRole("button", { name: "取消 task-queued" });
    await user.click(cancel);
    expect(await screen.findByText("任务取消失败，请重试。")).toBeVisible();
    expect(screen.queryByText(/private|upstream|raw/i)).not.toBeInTheDocument();
    expect(cancel).toBeEnabled();
    expect(screen.getAllByText("排队中").some((element) => element.matches(".status"))).toBe(true);

    await user.click(cancel);
    await waitFor(() => expect(cancelTask).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getAllByText("已取消").some((element) => element.matches(".status"))).toBe(true));
  });
});
