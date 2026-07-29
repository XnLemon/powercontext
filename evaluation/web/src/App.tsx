import { useCallback, useEffect, useState } from "react";

import { EvaluationApi } from "./api";
import { AppShell } from "./components/AppShell";
import { TaskForm } from "./components/TaskForm";

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

interface Route {
  batchId: string | null;
  taskId: string | null;
  page: "overview" | "tasks" | "task";
}

function parseRoute(path: string): Route {
  const taskMatch = path.match(/^\/report\/([^/]+)\/tasks\/([^/]+)$/);
  if (taskMatch?.[1] && taskMatch[2]) {
    return {
      batchId: decodeURIComponent(taskMatch[1]),
      taskId: decodeURIComponent(taskMatch[2]),
      page: "task",
    };
  }
  const tasksMatch = path.match(/^\/report\/([^/]+)\/tasks$/);
  if (tasksMatch?.[1]) {
    return { batchId: decodeURIComponent(tasksMatch[1]), taskId: null, page: "tasks" };
  }
  const overviewMatch = path.match(/^\/report\/([^/]+)$/);
  if (overviewMatch?.[1]) {
    return { batchId: decodeURIComponent(overviewMatch[1]), taskId: null, page: "overview" };
  }
  return { batchId: null, taskId: null, page: "overview" };
}

export function App({ api: injectedApi }: AppProps) {
  const [defaultApi] = useState(() => new EvaluationApi());
  const api = injectedApi ?? defaultApi;
  const [path, navigate] = usePath();
  const route = parseRoute(path);

  let content;
  if (route.page === "task" && route.batchId !== null && route.taskId !== null) {
    content = (
      <div className="page">
        <PageHeader eyebrow="任务详细报告" title="单任务详情" />
        <section className="panel state-message">正在读取任务 {route.taskId}…</section>
      </div>
    );
  } else if (route.page === "tasks" && route.batchId !== null) {
    content = (
      <div className="page">
        <PageHeader eyebrow={route.batchId} title="任务详细报告" description="逐项比较 OFF / ON 的客观结果。" />
        <section className="panel state-message">正在读取任务列表…</section>
      </div>
    );
  } else if (route.batchId !== null) {
    content = (
      <div className="page">
        <PageHeader eyebrow={route.batchId} title="总体报告" />
        <section className="panel state-message">正在读取批次报告…</section>
      </div>
    );
  } else {
    content = (
      <div className="page">
        <PageHeader
          eyebrow="PowerContext Evaluation"
          title="总体报告"
          description="选择已有批次，或提交一次固定 731 任务的完整 OFF / ON 评测。"
        />
        <TaskForm
          api={api}
          onCreated={(batch) => navigate(`/report/${encodeURIComponent(batch.batch_id)}`)}
        />
      </div>
    );
  }

  return (
    <AppShell api={api} path={path} batchId={route.batchId} navigate={navigate}>
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
