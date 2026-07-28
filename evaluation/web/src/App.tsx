import { useCallback, useEffect, useRef, useState } from "react";

import { EvaluationApi } from "./api";
import { AppShell } from "./components/AppShell";
import { TaskDetail } from "./components/TaskDetail";
import { TaskForm } from "./components/TaskForm";
import { TaskList } from "./components/TaskList";
import { ReportView } from "./components/ReportView";
import { ReportIndex } from "./components/ReportIndex";
import type { TaskRecord, TaskSummary } from "./types";

interface AppProps {
  api?: EvaluationApi;
}

function usePath(): [string, (next: string) => void] {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = useCallback((next: string) => {
    if (next !== window.location.pathname) window.history.pushState({}, "", next);
    setPath(next);
  }, []);
  return [path, navigate];
}

export function App({ api: injectedApi }: AppProps) {
  const [defaultApi] = useState(() => new EvaluationApi());
  const api = injectedApi ?? defaultApi;
  const [path, navigate] = usePath();

  let content;
  const taskMatch = path.match(/^\/tasks\/([^/]+)$/);
  const reportMatch = path.match(/^\/reports\/([^/]+)$/);
  if (taskMatch?.[1]) {
    content = (
      <div className="page">
        <PageHeader eyebrow="测试任务" title="任务详情" />
        <TaskDetail api={api} taskId={decodeURIComponent(taskMatch[1])} />
      </div>
    );
  } else if (path === "/tasks") {
    content = (
      <div className="page">
        <PageHeader eyebrow="队列" title="测试任务" description="查看已提交任务的当前阶段与最终结果。" />
        <TaskList api={api} onSelect={(taskId) => navigate(`/tasks/${encodeURIComponent(taskId)}`)} />
      </div>
    );
  } else if (reportMatch?.[1]) {
    const taskId = decodeURIComponent(reportMatch[1]);
    content = (
      <div className="page">
        <PageHeader eyebrow="只读结果" title="验收报告" />
        <ReportView api={api} taskId={taskId} />
      </div>
    );
  } else if (path === "/reports" || path.startsWith("/reports/")) {
    content = (
      <div className="page">
        <PageHeader eyebrow="只读结果" title="验收报告" />
        <ReportIndex api={api} />
      </div>
    );
  } else {
    content = <Workbench api={api} navigate={navigate} />;
  }

  return (
    <AppShell api={api} path={path} navigate={navigate}>
      {content}
    </AppShell>
  );
}

function PageHeader({ eyebrow, title, description }: { eyebrow: string; title: string; description?: string }) {
  return (
    <header className="page-header">
      <p className="eyebrow">{eyebrow}</p>
      <h1>{title}</h1>
      {description && <p>{description}</p>}
    </header>
  );
}

function Workbench({ api, navigate }: { api: EvaluationApi; navigate(path: string): void }) {
  const [focusTask, setFocusTask] = useState<TaskRecord | null>(null);
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [overviewError, setOverviewError] = useState(false);
  const overviewGeneration = useRef(0);
  const overviewController = useRef<AbortController | null>(null);
  const loadOverview = useCallback(() => {
    overviewController.current?.abort();
    const controller = new AbortController();
    overviewController.current = controller;
    const generation = ++overviewGeneration.current;
    setOverviewError(false);
    api
      .listTasks({ order: "newest", limit: 50, offset: 0 }, controller.signal)
      .then((nextTasks) => {
        if (!controller.signal.aborted && generation === overviewGeneration.current) setTasks(nextTasks);
      })
      .catch(() => {
        if (!controller.signal.aborted && generation === overviewGeneration.current) setOverviewError(true);
      });
  }, [api]);
  useEffect(() => {
    loadOverview();
    return () => {
      overviewController.current?.abort();
      overviewGeneration.current += 1;
    };
  }, [loadOverview]);

  const running = tasks?.find((task) => task.status === "running");
  const latestSucceeded = tasks
    ?.filter((task) => task.status === "succeeded")
    .reduce<TaskSummary | undefined>((latest, task) => {
      if (latest === undefined) return task;
      const taskTime = Date.parse(task.finished_at ?? task.created_at);
      const latestTime = Date.parse(latest.finished_at ?? latest.created_at);
      return taskTime > latestTime ? task : latest;
    }, undefined);
  const selectedId = focusTask?.task_id ?? running?.task_id;

  return (
    <div className="page">
      <PageHeader eyebrow="工作台" title="评测工作台" description="提交固定范围的 SWE-bench Pro OFF / ON 对照任务。" />
      <div className="workbench-grid">
        <TaskForm
          api={api}
          onCreated={(task) => {
            setFocusTask(task);
            loadOverview();
          }}
        />
        <div className="workbench-focus">
          {selectedId ? (
            <>
              <h2 className="focus-heading">{focusTask ? "已提交任务" : "当前运行任务"}</h2>
              <TaskDetail api={api} taskId={selectedId} onTaskChanged={loadOverview} />
            </>
          ) : overviewError ? (
            <section className="panel empty-state">
              <p>任务概览暂时无法加载。</p>
              <button type="button" className="secondary-button" onClick={loadOverview}>
                重试
              </button>
            </section>
          ) : tasks === null ? (
            <section className="panel state-message">正在加载任务概览…</section>
          ) : latestSucceeded ? (
            <section className="panel latest-report">
              <p className="eyebrow">上次运行</p>
              <h2>最近完成</h2>
              <p className="task-id">{latestSucceeded.task_id}</p>
              <p>报告已生成，可进入只读报告页查看系统产出。</p>
              <a className="primary-link" href={`/reports/${encodeURIComponent(latestSucceeded.task_id)}`}>
                查看验收报告
              </a>
            </section>
          ) : (
            <section className="panel empty-state">
              <h2>等待第一个任务</h2>
              <p>提交任务后，这里会显示当前阶段。完成后可从任务详情进入验收报告。</p>
              <button type="button" className="text-button" onClick={() => navigate("/tasks")}>
                查看任务队列
              </button>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
