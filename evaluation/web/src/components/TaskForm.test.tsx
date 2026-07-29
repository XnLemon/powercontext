import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TaskForm } from "./TaskForm";
import { apiStub, batchRecord } from "../test/fixtures";

describe("TaskForm complete batch request", () => {
  it("presents one fixed 731-task evaluation without an instance selector", () => {
    render(<TaskForm api={apiStub()} onCreated={() => undefined} />);

    expect(screen.getByText("SWE-bench Pro public v2")).toBeVisible();
    expect(screen.getByText("731 个任务，每个任务依次运行 OFF / ON")).toBeVisible();
    expect(screen.getByText("gpt-5.6-sol · medium")).toBeVisible();
    expect(screen.queryByLabelText("测试实例")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/命令|参数|自定义/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "运行完整评测" })).toBeEnabled();
  });

  it("submits the exact fixed batch contract and reports queueing", async () => {
    const createBatch = vi.fn().mockResolvedValue(batchRecord({ batch_id: "batch-created" }));
    const onCreated = vi.fn();
    render(<TaskForm api={apiStub({ createBatch })} onCreated={onCreated} />);

    fireEvent.click(screen.getByRole("button", { name: "运行完整评测" }));

    await waitFor(() => expect(createBatch).toHaveBeenCalledTimes(1));
    expect(createBatch).toHaveBeenCalledWith(
      {
        powercontext_ref: "latest",
        benchmark: "swebench-pro",
        task_set: "swebench-pro-public-v2",
        model: "gpt-5.6-sol",
        reasoning_effort: "medium",
        treatment_mode: "off_on",
        idempotency_key: expect.stringMatching(/^[A-Za-z0-9._-]{8,128}$/),
      },
      expect.any(AbortSignal),
    );
    expect(await screen.findByText("batch-created")).toBeVisible();
    expect(screen.getByText(/已提交完整评测/)).toBeVisible();
    expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ batch_id: "batch-created" }));
  });

  it("validates revisions and reuses an idempotency key until the intent changes", async () => {
    const user = userEvent.setup();
    const createBatch = vi
      .fn()
      .mockRejectedValueOnce(new Error("network"))
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(batchRecord({ batch_id: "batch-success" }));
    render(<TaskForm api={apiStub({ createBatch })} onCreated={() => undefined} />);
    const revision = screen.getByLabelText("PowerContext 版本");
    const submit = screen.getByRole("button", { name: "运行完整评测" });

    await user.clear(revision);
    await user.type(revision, "main; shutdown");
    await user.click(submit);
    expect(await screen.findByText("请输入 latest 或 commit: 开头的 40 位提交哈希。")).toBeVisible();
    expect(createBatch).not.toHaveBeenCalled();

    await user.clear(revision);
    await user.type(revision, "latest");
    await user.click(submit);
    expect(await screen.findByText("提交失败，请稍后重试。")).toBeVisible();
    const firstKey = createBatch.mock.calls[0]?.[0].idempotency_key;
    await user.click(submit);
    await waitFor(() => expect(createBatch).toHaveBeenCalledTimes(2));
    expect(createBatch.mock.calls[1]?.[0].idempotency_key).toBe(firstKey);

    await user.clear(revision);
    await user.type(revision, `commit:${"a".repeat(40)}`);
    await user.click(submit);
    expect(await screen.findByText(/batch-success/)).toBeVisible();
    expect(createBatch.mock.calls[2]?.[0].idempotency_key).not.toBe(firstKey);
  });
});
