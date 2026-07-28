import { useEffect, useState, type MouseEvent, type ReactNode } from "react";

import type { EvaluationApi } from "../api";
import type { HealthResponse } from "../types";

interface AppShellProps {
  api: EvaluationApi;
  path: string;
  navigate(path: string): void;
  children: ReactNode;
}

export function AppShell({ api, path, navigate, children }: AppShellProps) {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    api
      .getHealth(controller.signal)
      .then((value) => {
        setHealth(value);
        setHealthError(false);
      })
      .catch(() => {
        if (!controller.signal.aborted) setHealthError(true);
      });
    return () => controller.abort();
  }, [api]);

  const links = [
    { href: "/", label: "工作台", current: path === "/" },
    { href: "/tasks", label: "测试任务", current: path.startsWith("/tasks") },
    { href: "/reports", label: "验收报告", current: path.startsWith("/reports") },
  ];
  const onLink = (event: MouseEvent<HTMLAnchorElement>, href: string) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(href);
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="/" onClick={(event) => onLink(event, "/")}>
          <span className="brand-mark" aria-hidden="true">
            PC
          </span>
          <span>PowerContext</span>
          <small>Evaluation Console</small>
        </a>
        <nav aria-label="主导航">
          {links.map((link) => (
            <a
              className="nav-link"
              href={link.href}
              aria-current={link.current ? "page" : undefined}
              onClick={(event) => onLink(event, link.href)}
              key={link.href}
            >
              {link.label}
            </a>
          ))}
        </nav>
      </aside>
      <div className="app-body">
        <header className="environment-bar" aria-label="运行环境">
          <span className="environment-name">m0</span>
          {healthError ? (
            <span className="health health--error">服务状态未知</span>
          ) : health === null ? (
            <span className="health">正在检查服务…</span>
          ) : (
            <>
              <span className="health health--ok">服务正常</span>
              <span className={`health ${health.worker_lease_active ? "health--ok" : "health--idle"}`}>
                {health.worker_lease_active ? "Worker 在线" : "Worker 未连接"}
              </span>
              <span className="queue-count">队列 {health.queued_tasks}</span>
            </>
          )}
        </header>
        <main className="main-content">{children}</main>
      </div>
    </div>
  );
}
