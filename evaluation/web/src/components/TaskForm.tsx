import { useEffect, useRef, useState, type FormEvent } from "react";

import type { EvaluationApi } from "../api";
import type { Capabilities, TaskCreate, TaskRecord } from "../types";

interface TaskFormProps {
  api: EvaluationApi;
  onCreated(task: TaskRecord): void;
}

const revisionPattern = /^(latest|commit:[0-9a-fA-F]{40})$/;

function hasOption(options: readonly string[], value: string): boolean {
  return options.includes(value);
}

function idempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") return `web-${crypto.randomUUID()}`;
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function TaskForm({ api, onCreated }: TaskFormProps) {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [loadingError, setLoadingError] = useState(false);
  const [revision, setRevision] = useState("latest");
  const [benchmark, setBenchmark] = useState("");
  const [instanceId, setInstanceId] = useState("");
  const [model, setModel] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState("");
  const [treatmentMode, setTreatmentMode] = useState("");
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState("");
  const [created, setCreated] = useState<TaskRecord | null>(null);
  const intentKey = useRef<{ fingerprint: string; key: string } | null>(null);

  const load = () => {
    setLoadingError(false);
    api
      .getCapabilities()
      .then((next) => {
        setCapabilities(next);
        setBenchmark((value) => (hasOption(next.benchmarks, value) ? value : (next.benchmarks[0] ?? "")));
        setInstanceId((value) => (hasOption(next.instances, value) ? value : (next.instances[0] ?? "")));
        setModel((value) => (hasOption(next.models, value) ? value : (next.models[0] ?? "")));
        setReasoningEffort((value) =>
          hasOption(next.reasoning_efforts, value) ? value : (next.reasoning_efforts[0] ?? ""),
        );
        setTreatmentMode((value) =>
          hasOption(next.treatment_modes, value) ? value : (next.treatment_modes[0] ?? ""),
        );
      })
      .catch(() => setLoadingError(true));
  };
  useEffect(load, [api]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setMessage("");
    if (!revisionPattern.test(revision)) {
      setMessage("请输入 latest 或 commit: 开头的 40 位提交哈希。");
      return;
    }
    if (capabilities === null) return;
    if (
      !hasOption(capabilities.benchmarks, benchmark) ||
      !hasOption(capabilities.instances, instanceId) ||
      !hasOption(capabilities.models, model) ||
      !hasOption(capabilities.reasoning_efforts, reasoningEffort) ||
      !hasOption(capabilities.treatment_modes, treatmentMode)
    ) {
      setMessage("当前没有可提交的固定测试配置。");
      return;
    }
    const intent = {
      powercontext_ref: revision,
      benchmark,
      instance_id: instanceId,
      model,
      reasoning_effort: reasoningEffort,
      treatment_mode: treatmentMode,
    } as const;
    const fingerprint = JSON.stringify(intent);
    if (intentKey.current?.fingerprint !== fingerprint) {
      intentKey.current = { fingerprint, key: idempotencyKey() };
    }
    const task = { ...intent, idempotency_key: intentKey.current.key } as unknown as TaskCreate;
    setPending(true);
    try {
      const result = await api.createTask(task);
      intentKey.current = null;
      setCreated(result);
      onCreated(result);
    } catch {
      setMessage("提交失败，请稍后重试。");
    } finally {
      setPending(false);
    }
  };

  if (loadingError) {
    return (
      <section className="panel task-form-panel">
        <h2>提交测试</h2>
        <p className="state-message">可用配置暂时无法加载。</p>
        <button type="button" className="secondary-button" onClick={load}>
          重试
        </button>
      </section>
    );
  }
  if (capabilities === null) {
    return (
      <section className="panel task-form-panel">
        <h2>提交测试</h2>
        <p className="state-message">正在读取可用配置…</p>
      </section>
    );
  }

  return (
    <section className="panel task-form-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">新任务</p>
          <h2>提交测试</h2>
        </div>
        <span className="safe-badge">固定能力集</span>
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
        <label>
          基准测试
          <select
            value={benchmark}
            onChange={(event) => {
              intentKey.current = null;
              setBenchmark(event.target.value);
            }}
          >
            {capabilities.benchmarks.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        <label>
          测试实例
          <select
            value={instanceId}
            onChange={(event) => {
              intentKey.current = null;
              setInstanceId(event.target.value);
            }}
          >
            {capabilities.instances.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        <div className="form-row">
          <label>
            模型
            <select
              value={model}
              onChange={(event) => {
                intentKey.current = null;
                setModel(event.target.value);
              }}
            >
              {capabilities.models.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
          <label>
            推理强度
            <select
              value={reasoningEffort}
              onChange={(event) => {
                intentKey.current = null;
                setReasoningEffort(event.target.value);
              }}
            >
              {capabilities.reasoning_efforts.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
        </div>
        <label>
          测试方式
          <select
            value={treatmentMode}
            onChange={(event) => {
              intentKey.current = null;
              setTreatmentMode(event.target.value);
            }}
          >
            {capabilities.treatment_modes.map((value) => (
              <option value={value} key={value}>
                {value === "off_on" ? "OFF / ON 对照" : value}
              </option>
            ))}
          </select>
        </label>
        <button className="primary-button" type="submit" disabled={pending}>
          {pending ? "正在提交…" : "提交测试任务"}
        </button>
      </form>
      <div className="form-feedback" aria-live="polite">
        {message && <p className="error-message">{message}</p>}
        {created && (
          <p className="success-message">
            已提交任务 <strong>{created.task_id}</strong>
            {created.queue_position !== null && <> · 队列位置：{created.queue_position}</>}
          </p>
        )}
      </div>
    </section>
  );
}
