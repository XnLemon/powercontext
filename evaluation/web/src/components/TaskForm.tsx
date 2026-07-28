import { useEffect, useRef, useState, type FormEvent } from "react";

import type { EvaluationApi } from "../api";
import type { Capabilities, TaskCreate, TaskRecord } from "../types";

interface TaskFormProps {
  api: EvaluationApi;
  onCreated(task: TaskRecord): void;
}

const revisionPattern = /^(latest|commit:[0-9a-fA-F]{40})$/;

function isOption<Option extends string>(options: readonly Option[], value: string): value is Option {
  return options.some((option) => option === value);
}

function idempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") return `web-${crypto.randomUUID()}`;
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function TaskForm({ api, onCreated }: TaskFormProps) {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [loadingError, setLoadingError] = useState(false);
  const [revision, setRevision] = useState("latest");
  const [benchmark, setBenchmark] = useState<TaskCreate["benchmark"] | "">("");
  const [instanceId, setInstanceId] = useState<TaskCreate["instance_id"] | "">("");
  const [model, setModel] = useState<TaskCreate["model"] | "">("");
  const [reasoningEffort, setReasoningEffort] = useState<TaskCreate["reasoning_effort"] | "">("");
  const [treatmentMode, setTreatmentMode] = useState<TaskCreate["treatment_mode"] | "">("");
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState("");
  const [created, setCreated] = useState<TaskRecord | null>(null);
  const intentKey = useRef<{ fingerprint: string; key: string } | null>(null);
  const capabilityGeneration = useRef(0);
  const capabilityController = useRef<AbortController | null>(null);
  const submitGeneration = useRef(0);
  const submitController = useRef<AbortController | null>(null);

  const load = () => {
    capabilityController.current?.abort();
    const controller = new AbortController();
    capabilityController.current = controller;
    const generation = ++capabilityGeneration.current;
    setLoadingError(false);
    api
      .getCapabilities(controller.signal)
      .then((next) => {
        if (controller.signal.aborted || generation !== capabilityGeneration.current) return;
        setCapabilities(next);
        setBenchmark((value) => (isOption(next.benchmarks, value) ? value : (next.benchmarks[0] ?? "")));
        setInstanceId((value) => (isOption(next.instances, value) ? value : (next.instances[0] ?? "")));
        setModel((value) => (isOption(next.models, value) ? value : (next.models[0] ?? "")));
        setReasoningEffort((value) =>
          isOption(next.reasoning_efforts, value) ? value : (next.reasoning_efforts[0] ?? ""),
        );
        setTreatmentMode((value) =>
          isOption(next.treatment_modes, value) ? value : (next.treatment_modes[0] ?? ""),
        );
      })
      .catch(() => {
        if (!controller.signal.aborted && generation === capabilityGeneration.current) setLoadingError(true);
      });
  };
  useEffect(() => {
    setPending(false);
    load();
    return () => {
      capabilityController.current?.abort();
      submitController.current?.abort();
      capabilityGeneration.current += 1;
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
    if (capabilities === null) return;
    if (
      !isOption(capabilities.benchmarks, benchmark) ||
      !isOption(capabilities.instances, instanceId) ||
      !isOption(capabilities.models, model) ||
      !isOption(capabilities.reasoning_efforts, reasoningEffort) ||
      !isOption(capabilities.treatment_modes, treatmentMode)
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
    const task: TaskCreate = { ...intent, idempotency_key: intentKey.current.key };
    submitController.current?.abort();
    const controller = new AbortController();
    submitController.current = controller;
    const generation = ++submitGeneration.current;
    setPending(true);
    try {
      const result = await api.createTask(task, controller.signal);
      if (controller.signal.aborted || generation !== submitGeneration.current) return;
      intentKey.current = null;
      setCreated(result);
      onCreated(result);
    } catch {
      if (!controller.signal.aborted && generation === submitGeneration.current) setMessage("提交失败，请稍后重试。");
    } finally {
      if (!controller.signal.aborted && generation === submitGeneration.current) setPending(false);
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
              if (isOption(capabilities.benchmarks, event.target.value)) {
                intentKey.current = null;
                setBenchmark(event.target.value);
              }
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
              if (isOption(capabilities.instances, event.target.value)) {
                intentKey.current = null;
                setInstanceId(event.target.value);
              }
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
                if (isOption(capabilities.models, event.target.value)) {
                  intentKey.current = null;
                  setModel(event.target.value);
                }
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
                if (isOption(capabilities.reasoning_efforts, event.target.value)) {
                  intentKey.current = null;
                  setReasoningEffort(event.target.value);
                }
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
              if (isOption(capabilities.treatment_modes, event.target.value)) {
                intentKey.current = null;
                setTreatmentMode(event.target.value);
              }
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
