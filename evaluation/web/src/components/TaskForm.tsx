import { useEffect, useRef, useState, type FormEvent } from "react";

import type { EvaluationApi } from "../api";
import type { BatchCreate, BatchRecord } from "../types";

interface TaskFormProps {
  api: EvaluationApi;
  onCreated(batch: BatchRecord): void;
}

const revisionPattern = /^(latest|commit:[0-9a-fA-F]{40})$/;

function idempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") return `web-${crypto.randomUUID()}`;
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function TaskForm({ api, onCreated }: TaskFormProps) {
  const [revision, setRevision] = useState("latest");
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState("");
  const [created, setCreated] = useState<BatchRecord | null>(null);
  const intentKey = useRef<{ revision: string; key: string } | null>(null);
  const submitGeneration = useRef(0);
  const submitController = useRef<AbortController | null>(null);

  useEffect(() => {
    setPending(false);
    return () => {
      submitController.current?.abort();
      submitGeneration.current += 1;
    };
  }, [api]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setMessage("");
    if (!revisionPattern.test(revision)) {
      setMessage("请输入 latest 或 commit: 开头的 40 位提交哈希。");
      return;
    }
    if (intentKey.current?.revision !== revision) {
      intentKey.current = { revision, key: idempotencyKey() };
    }
    const request: BatchCreate = {
      powercontext_ref: revision,
      benchmark: "swebench-pro",
      task_set: "swebench-pro-public-v2",
      model: "gpt-5.6-sol",
      reasoning_effort: "medium",
      treatment_mode: "off_on",
      idempotency_key: intentKey.current.key,
    };
    submitController.current?.abort();
    const controller = new AbortController();
    submitController.current = controller;
    const generation = ++submitGeneration.current;
    setPending(true);
    try {
      const result = await api.createBatch(request, controller.signal);
      if (controller.signal.aborted || generation !== submitGeneration.current) return;
      intentKey.current = null;
      setCreated(result);
      onCreated(result);
    } catch {
      if (!controller.signal.aborted && generation === submitGeneration.current) {
        setMessage("提交失败，请稍后重试。");
      }
    } finally {
      if (!controller.signal.aborted && generation === submitGeneration.current) setPending(false);
    }
  };

  return (
    <section className="panel task-form-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">新批次</p>
          <h2>运行完整评测</h2>
        </div>
        <span className="safe-badge">固定任务集</span>
      </div>
      <div className="batch-contract" aria-label="固定评测范围">
        <strong>SWE-bench Pro public v2</strong>
        <span>731 个任务，每个任务依次运行 OFF / ON</span>
        <span>gpt-5.6-sol · medium</span>
        <span>全局同时只运行一个任务，其余任务排队</span>
      </div>
      <form onSubmit={submit}>
        <label>
          PowerContext 版本
          <input
            aria-label="PowerContext 版本"
            value={revision}
            onChange={(event) => {
              intentKey.current = null;
              setRevision(event.target.value);
            }}
            spellCheck={false}
          />
          <span className="field-hint">latest 或 commit: 加 40 位提交哈希</span>
        </label>
        <button className="primary-button" type="submit" disabled={pending}>
          {pending ? "正在提交…" : "运行完整评测"}
        </button>
      </form>
      <div className="form-feedback" aria-live="polite">
        {message && <p className="error-message">{message}</p>}
        {created && (
          <p className="success-message">
            已提交完整评测 <strong>{created.batch_id}</strong> · {created.total_tasks} 个任务
          </p>
        )}
      </div>
    </section>
  );
}
