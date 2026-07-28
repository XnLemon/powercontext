import { useCallback, useEffect, useState } from "react";

import type { EvaluationApi } from "../api";
import type { TaskPhase, TaskStatus, TaskSummary } from "../types";

export const statusLabels: Record<TaskStatus, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  interrupted: "已中断",
  cancelled: "已取消",
};

export const phaseLabels: Record<TaskPhase, string> = {
  preparing: "准备环境",
  validating_gold: "验证 Gold",
  running_off: "OFF 执行",
  running_on: "ON 执行",
  official_evaluation: "官方评测",
  generating_report: "生成报告",
};

interface TaskListProps {
  api: EvaluationApi;
  onSelect(taskId: string): void;
}

function time(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "short",
    timeStyle: "short",
    hour12: false,
  }).format(new Date(value));
}

export function TaskList({ api, onSelect }: TaskListProps) {
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [filter, setFilter] = useState<TaskStatus | "">("");
  const [error, setError] = useState(false);
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState("");

  const load = useCallback(async () => {
    setError(false);
    try {
      const options = { limit: 50, offset: 0, ...(filter === "" ? {} : { status: filter }) };
      setTasks(await api.listTasks(options));
    } catch {
      setError(true);
    }
  }, [api, filter]);
  useEffect(() => void load(), [load]);

  const cancel = async (taskId: string) => {
    if (!window.confirm(`确定取消排队中的任务 ${taskId}？`)) return;
    setCancelling(taskId);
    setCancelError("");
    try {
      await api.cancelTask(taskId);
      await load();
    } catch {
      setCancelError("任务取消失败，请重试。");
    } finally {
      setCancelling(null);
    }
  };

  return (
    <section className="panel task-list-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">任务队列</p>
          <h2>全部任务</h2>
        </div>
        <label className="filter-control">
          状态筛选
          <select value={filter} onChange={(event) => setFilter(event.target.value as TaskStatus | "")}>
            <option value="">全部状态</option>
            {Object.entries(statusLabels).map(([value, label]) => (
              <option value={value} key={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="list-feedback" aria-live="polite">
        {cancelError && <p className="error-message">{cancelError}</p>}
      </div>
      {error ? (
        <div className="empty-state">
          <p>任务列表暂时无法加载。</p>
          <button className="secondary-button" type="button" onClick={() => void load()}>
            重试
          </button>
        </div>
      ) : tasks === null ? (
        <p className="state-message">正在加载任务…</p>
      ) : tasks.length === 0 ? (
        <p className="empty-state">{filter ? "没有符合条件的任务。" : "还没有测试任务。"}</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>任务 / 版本</th>
                <th>实例 / 模型</th>
                <th>提交时间</th>
                <th>状态</th>
                <th>阶段 / 结果</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr className={task.status === "running" ? "task-row--running" : undefined} key={task.task_id}>
                  <td>
                    <button className="task-link" type="button" onClick={() => onSelect(task.task_id)}>
                      {task.task_id}
                    </button>
                    <small>{task.powercontext_ref}</small>
                  </td>
                  <td>
                    <span className="truncate">{task.instance_id}</span>
                    <small>{task.model}</small>
                  </td>
                  <td>{time(task.created_at)}</td>
                  <td>
                    <span className={`status status--${task.status}`}>{statusLabels[task.status]}</span>
                    {task.queue_position !== null && <small>队列第 {task.queue_position} 位</small>}
                  </td>
                  <td>
                    {task.phase ? phaseLabels[task.phase] : "—"}
                    {task.status === "succeeded" && (
                      <small>
                        OFF {task.off_resolved ? "解决" : "未解决"} · ON {task.on_resolved ? "解决" : "未解决"}
                      </small>
                    )}
                  </td>
                  <td>
                    {task.status === "queued" && (
                      <button
                        className="text-button text-button--danger"
                        type="button"
                        disabled={cancelling === task.task_id}
                        aria-label={`取消 ${task.task_id}`}
                        onClick={() => void cancel(task.task_id)}
                      >
                        取消
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
