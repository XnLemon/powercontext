import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TaskRunDetail } from "./TaskRunDetail";
import { apiStub, batchRecord, batchTaskDetail } from "../test/fixtures";

describe("TaskRunDetail", () => {
  it("renders official result evidence once and expands the complete task in place", async () => {
    const longProblem = "完整问题：" + "需要保留的上下文。".repeat(80);
    const api = apiStub({
      getBatch: vi.fn().mockResolvedValue(
        batchRecord({
          status: "completed",
          resolved_powercontext_sha: "a".repeat(40),
        }),
      ),
      getBatchTask: vi.fn().mockResolvedValue({ ...batchTaskDetail, problem_statement: longProblem }),
    });
    render(
      <TaskRunDetail
        api={api}
        batchId="batch-001"
        taskId="task-001"
        search="?category=off_pass_on_fail&sort=token_delta_desc"
        navigate={() => undefined}
      />,
    );

    expect(await screen.findByRole("heading", { name: "单任务详情" })).toBeVisible();
    expect(screen.getByText("instance_owner__repo-001")).toBeVisible();
    expect(screen.getByText("owner/repo")).toBeVisible();
    expect(screen.getByText("OFF 通过")).toBeVisible();
    expect(screen.getByText("ON 未通过")).toBeVisible();
    expect(screen.getByText("OFF 110")).toBeVisible();
    expect(screen.getByText("ON 135")).toBeVisible();
    expect(screen.getByText("差值 +25")).toBeVisible();
    expect(screen.getByRole("link", { name: "返回任务详细报告" })).toHaveAttribute(
      "href",
      "/report/batch-001/tasks?category=off_pass_on_fail&sort=token_delta_desc",
    );

    expect(screen.queryByText(longProblem)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开完整任务描述" }));
    expect(screen.getByText(longProblem)).toBeVisible();

    const off = screen.getByLabelText("OFF 官方评测");
    expect(within(off).getByText("补丁应用成功")).toBeVisible();
    expect(within(off).getByText("FAIL_TO_PASS 1 / 1")).toBeVisible();
    expect(within(off).getByText("PASS_TO_PASS 1 / 1")).toBeVisible();
    expect(within(off).getAllByText("已解决")).toHaveLength(1);

    const on = screen.getByLabelText("ON 官方评测");
    expect(within(on).getByText("补丁应用成功")).toBeVisible();
    expect(within(on).getByText("FAIL_TO_PASS 0 / 1")).toBeVisible();
    expect(within(on).getByText("失败测试：test_issue")).toBeVisible();
    expect(within(on).getByText("test_issue failed")).toBeVisible();
    expect(within(on).getAllByText("未解决")).toHaveLength(1);
    expect(screen.queryByText(/生命周期|处理有效性|补丁大小|N\/A|验收/)).not.toBeInTheDocument();
  });
});
