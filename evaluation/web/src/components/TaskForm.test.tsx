import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TaskForm } from "./TaskForm";
import { apiStub, capabilities, instanceId, record } from "../test/fixtures";

describe("TaskForm", () => {
  it("loads exact server capabilities and defaults to OFF/ON", async () => {
    render(<TaskForm api={apiStub()} onCreated={() => undefined} />);

    expect(screen.getByText("正在读取可用配置…")).toBeVisible();
    expect(await screen.findByRole("option", { name: "swebench-pro" })).toBeVisible();
    expect(screen.getByLabelText("测试实例")).toHaveValue(instanceId);
    expect(screen.getByLabelText("模型")).toHaveValue("gpt-5.6-sol");
    expect(screen.getByLabelText("推理强度")).toHaveValue("medium");
    expect(screen.getByLabelText("测试方式")).toHaveValue("off_on");
    expect(screen.queryByLabelText(/命令|参数|自定义/i)).not.toBeInTheDocument();
  });

  it("submits exact safe values by keyboard and only disables while POST is pending", async () => {
    const user = userEvent.setup();
    let resolveCreate!: (value: ReturnType<typeof record>) => void;
    const createTask = vi.fn().mockReturnValue(
      new Promise((resolve) => {
        resolveCreate = resolve;
      }),
    );
    const onCreated = vi.fn();
    const api = apiStub({ createTask });
    render(<TaskForm api={api} onCreated={onCreated} />);
    await screen.findByRole("option", { name: "swebench-pro" });

    const revision = screen.getByLabelText("PowerContext 版本");
    await user.clear(revision);
    await user.type(revision, `commit:${"a".repeat(40)}{Enter}`);

    await waitFor(() => expect(createTask).toHaveBeenCalledTimes(1));
    expect(createTask).toHaveBeenCalledWith({
      powercontext_ref: `commit:${"a".repeat(40)}`,
      benchmark: capabilities.benchmarks[0],
      instance_id: capabilities.instances[0],
      model: capabilities.models[0],
      reasoning_effort: capabilities.reasoning_efforts[0],
      treatment_mode: "off_on",
      idempotency_key: expect.stringMatching(/^[A-Za-z0-9._-]{8,128}$/),
    });
    expect(screen.getByRole("button", { name: "正在提交…" })).toBeDisabled();
    expect(revision).not.toBeDisabled();

    resolveCreate(record("queued", "task-created"));
    expect(await screen.findByText(/task-created/)).toBeVisible();
    expect(screen.getByText(/队列位置：2/)).toBeVisible();
    expect(screen.getByRole("button", { name: "提交测试任务" })).toBeEnabled();
    expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ task_id: "task-created" }));
  });

  it("rejects unsafe revisions and renders API errors as text", async () => {
    const createTask = vi.fn().mockRejectedValue(new Error("<script>private</script>"));
    render(<TaskForm api={apiStub({ createTask })} onCreated={() => undefined} />);
    await screen.findByRole("option", { name: "swebench-pro" });

    fireEvent.change(screen.getByLabelText("PowerContext 版本"), { target: { value: "main; shutdown" } });
    fireEvent.click(screen.getByRole("button", { name: "提交测试任务" }));
    expect(await screen.findByText("请输入 latest 或 commit: 开头的 40 位提交哈希。")).toBeVisible();
    expect(createTask).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("PowerContext 版本"), { target: { value: "latest" } });
    fireEvent.click(screen.getByRole("button", { name: "提交测试任务" }));
    expect(await screen.findByText("提交失败，请稍后重试。")).toBeVisible();
    expect(screen.queryByText(/private|script/i)).not.toBeInTheDocument();
  });
});
