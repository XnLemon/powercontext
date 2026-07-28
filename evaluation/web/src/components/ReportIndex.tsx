import { useCallback, useEffect, useRef, useState } from "react";

import type { EvaluationApi } from "../api";
import type { TaskSummary } from "../types";

export function ReportIndex({ api }: { api: EvaluationApi }) {
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [error, setError] = useState(false);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const load = useCallback(() => {
    controller.current?.abort();
    const nextController = new AbortController();
    controller.current = nextController;
    const requestGeneration = ++generation.current;
    setError(false);
    api.listTasks({ status: "succeeded", order: "newest", limit: 50, offset: 0 }, nextController.signal)
      .then((nextTasks) => {
        if (!nextController.signal.aborted && requestGeneration === generation.current) {
          setTasks(nextTasks.filter((task) => task.status === "succeeded"));
        }
      })
      .catch(() => {
        if (!nextController.signal.aborted && requestGeneration === generation.current) setError(true);
      });
  }, [api]);

  useEffect(() => {
    load();
    return () => {
      controller.current?.abort();
      generation.current += 1;
    };
  }, [load]);

  if (error) {
    return (
      <section className="panel empty-state">
        <p>验收报告列表暂时无法加载。</p>
        <button type="button" className="secondary-button" onClick={load}>重试</button>
      </section>
    );
  }
  if (tasks === null) return <section className="panel state-message">正在加载验收报告…</section>;
  if (tasks.length === 0) return <section className="panel empty-state">暂无已完成的验收报告。</section>;

  return (
    <section className="panel report-index" aria-labelledby="report-index-heading">
      <h2 id="report-index-heading">已完成报告</h2>
      <ul>
        {tasks.map((task, index) => (
          <li key={task.task_id}>
            <div>
              <strong>{task.task_id}{index === 0 && <span className="latest-label">最新报告</span>}</strong>
              <span>{task.powercontext_ref} · {task.model}</span>
            </div>
            <a
              className="primary-link"
              href={`/reports/${encodeURIComponent(task.task_id)}`}
              aria-label={`查看 ${task.task_id} 的验收报告`}
            >
              查看报告
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}
