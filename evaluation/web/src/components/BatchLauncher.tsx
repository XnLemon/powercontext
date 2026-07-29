import { useEffect, useRef, useState, type FormEvent } from "react";

import type { EvaluationApi } from "../api";
import type { BatchCreate, BatchPreview, BatchRecord } from "../types";

interface BatchLauncherProps {
  api: EvaluationApi;
  onCreated(batch: BatchRecord): void;
}

const revisionPattern = /^(latest|commit:[0-9a-fA-F]{40})$/;

function idempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") return `web-${crypto.randomUUID()}`;
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function number(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function dateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

export function BatchLauncher({ api, onCreated }: BatchLauncherProps) {
  const [revision, setRevision] = useState("latest");
  const [threshold, setThreshold] = useState(80);
  const [preview, setPreview] = useState<BatchPreview | null>(null);
  const [pending, setPending] = useState<"preview" | "submitting" | null>(null);
  const [message, setMessage] = useState("");
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const confirmationKey = useRef<{ revision: string; threshold: number; key: string } | null>(null);

  useEffect(
    () => () => {
      controller.current?.abort();
      generation.current += 1;
    },
    [],
  );

  const invalidatePreview = () => {
    controller.current?.abort();
    generation.current += 1;
    setPending(null);
    setPreview(null);
    setMessage("");
    confirmationKey.current = null;
  };

  const requestPreview = async (event: FormEvent) => {
    event.preventDefault();
    setMessage("");
    if (!revisionPattern.test(revision)) {
      setMessage("请输入 latest 或 commit: 开头的 40 位提交哈希。");
      return;
    }
    if (!Number.isInteger(threshold) || threshold < 1 || threshold > 100) {
      setMessage("暂停阈值必须是 1 到 100 之间的整数。");
      return;
    }
    controller.current?.abort();
    const nextController = new AbortController();
    controller.current = nextController;
    const currentGeneration = ++generation.current;
    setPending("preview");
    try {
      const result = await api.previewBatch(
        { powercontext_ref: revision, usage_pause_percent: threshold },
        nextController.signal,
      );
      if (nextController.signal.aborted || generation.current !== currentGeneration) return;
      confirmationKey.current = null;
      setPreview(result);
    } catch {
      if (!nextController.signal.aborted && generation.current === currentGeneration) {
        setMessage("当前无法读取 Codex 用量或评测预览，请稍后重试。");
      }
    } finally {
      if (!nextController.signal.aborted && generation.current === currentGeneration) setPending(null);
    }
  };

  const confirm = async () => {
    if (preview === null || !preview.can_start || pending !== null) return;
    const intent = { revision: preview.powercontext_ref, threshold: preview.usage_pause_percent };
    if (
      confirmationKey.current?.revision !== intent.revision
      || confirmationKey.current.threshold !== intent.threshold
    ) {
      confirmationKey.current = { ...intent, key: idempotencyKey() };
    }
    const request: BatchCreate = {
      powercontext_ref: preview.powercontext_ref,
      benchmark: preview.benchmark,
      task_set: preview.task_set,
      model: preview.model,
      reasoning_effort: preview.reasoning_effort,
      treatment_mode: preview.treatment_mode,
      usage_pause_percent: preview.usage_pause_percent,
      idempotency_key: confirmationKey.current.key,
    };
    controller.current?.abort();
    const nextController = new AbortController();
    controller.current = nextController;
    const currentGeneration = ++generation.current;
    setMessage("");
    setPending("submitting");
    try {
      const batch = await api.createBatch(request, nextController.signal);
      if (nextController.signal.aborted || generation.current !== currentGeneration) return;
      confirmationKey.current = null;
      onCreated(batch);
    } catch {
      if (!nextController.signal.aborted && generation.current === currentGeneration) {
        setMessage("提交失败，未创建新的确认意图；可以安全重试。");
      }
    } finally {
      if (!nextController.signal.aborted && generation.current === currentGeneration) setPending(null);
    }
  };

  return (
    <section className="panel task-form-panel batch-launcher">
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

      <form onSubmit={requestPreview} className="launcher-form">
        <label>
          PowerContext 版本
          <input
            aria-label="PowerContext 版本"
            value={revision}
            onChange={(event) => {
              invalidatePreview();
              setRevision(event.target.value);
            }}
            spellCheck={false}
          />
          <span className="field-hint">latest 或 commit: 加 40 位提交哈希</span>
        </label>
        <label>
          暂停阈值
          <span className="threshold-input">
            <input
              aria-label="暂停阈值"
              type="number"
              min={1}
              max={100}
              value={threshold}
              onChange={(event) => {
                invalidatePreview();
                setThreshold(event.target.valueAsNumber);
              }}
            />
            <span>%</span>
          </span>
          <span className="field-hint">达到阈值后，在当前完整 OFF / ON 任务结束时暂停</span>
        </label>
        <button className="primary-button" type="submit" disabled={pending !== null}>
          {pending === "preview" ? "正在读取…" : "预览评测"}
        </button>
      </form>

      {preview !== null && (
        <section className="launch-preview" aria-label="评测确认">
          <div className="launch-preview__head">
            <div>
              <p className="eyebrow">确认信息</p>
              <h3>{number(preview.total_tasks)} 个基准任务</h3>
            </div>
            <strong className="usage-reading">当前用量 {preview.usage.used_percent}%</strong>
          </div>
          <dl className="preview-facts">
            <div><dt>任务集</dt><dd>SWE-bench Pro public v2</dd></div>
            <div><dt>运行方式</dt><dd>每个任务 OFF / ON 配对执行</dd></div>
            <div><dt>暂停阈值</dt><dd>{preview.usage_pause_percent}%</dd></div>
            <div><dt>额度重置</dt><dd>{dateTime(preview.usage.resets_at)}</dd></div>
            <div><dt>用量采样</dt><dd>{dateTime(preview.usage.observed_at)}</dd></div>
            <div>
              <dt>剩余估算</dt>
              <dd>
                {preview.estimate.quality === "unavailable"
                  ? "暂无可靠估算"
                  : `${preview.estimate.quality === "preliminary" ? "初步估算" : "已测量"} · ${preview.estimate.sample_size} 个样本`}
              </dd>
            </div>
          </dl>
          {!preview.can_start && (
            <p className="usage-blocked">当前用量已达到暂停阈值</p>
          )}
          <button
            className="primary-button"
            type="button"
            disabled={!preview.can_start || pending !== null}
            onClick={confirm}
          >
            {pending === "submitting" ? "正在提交…" : "确认并开始评测"}
          </button>
        </section>
      )}

      <div className="form-feedback" aria-live="polite">
        {message && <p className="error-message">{message}</p>}
      </div>
    </section>
  );
}
