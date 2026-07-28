export type TaskStatus = "queued" | "running" | "succeeded" | "failed" | "interrupted" | "cancelled";

export type TaskPhase =
  | "preparing"
  | "validating_gold"
  | "running_off"
  | "running_on"
  | "official_evaluation"
  | "generating_report";

export type FailureCategory =
  | "invalid_request"
  | "queue_unavailable"
  | "source_resolution_failure"
  | "environment_preparation_failure"
  | "gold_validation_failure"
  | "codex_execution_failure"
  | "treatment_validation_failure"
  | "official_evaluator_failure"
  | "report_generation_failure"
  | "worker_interruption"
  | "internal";

export interface TaskCreate {
  powercontext_ref: string;
  benchmark: "swebench-pro";
  instance_id: "instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9";
  model: "gpt-5.6-sol";
  reasoning_effort: "medium";
  treatment_mode: "off_on";
  idempotency_key: string;
}

export interface TaskResult {
  artifact_dir: string;
  report_path: string;
  off_resolved: boolean;
  on_resolved: boolean;
}

interface TaskRecordBase {
  task_id: string;
  request: TaskCreate;
  created_at: string;
  version: number;
  queue_position: number | null;
}

export interface QueuedTaskRecord extends TaskRecordBase {
  status: "queued";
  phase: null;
  started_at: null;
  finished_at: null;
  failure_category: null;
  failure_phase: null;
  failure_summary: null;
  result: null;
}

export interface RunningTaskRecord extends TaskRecordBase {
  status: "running";
  phase: TaskPhase | null;
  started_at: string;
  finished_at: null;
  failure_category: null;
  failure_phase: null;
  failure_summary: null;
  result: null;
}

export interface SucceededTaskRecord extends TaskRecordBase {
  status: "succeeded";
  phase: TaskPhase | null;
  started_at: string;
  finished_at: string;
  failure_category: null;
  failure_phase: null;
  failure_summary: null;
  result: TaskResult;
}

export interface FailedTaskRecord extends TaskRecordBase {
  status: "failed" | "interrupted";
  phase: TaskPhase | null;
  started_at: string;
  finished_at: string;
  failure_category: FailureCategory;
  failure_phase: TaskPhase | null;
  failure_summary: string;
  result: null;
}

export interface CancelledTaskRecord extends TaskRecordBase {
  status: "cancelled";
  phase: null;
  started_at: null;
  finished_at: string;
  failure_category: null;
  failure_phase: null;
  failure_summary: null;
  result: null;
}

export type TaskRecord =
  | QueuedTaskRecord
  | RunningTaskRecord
  | SucceededTaskRecord
  | FailedTaskRecord
  | CancelledTaskRecord;

export interface TaskSummary {
  task_id: string;
  powercontext_ref: string;
  instance_id: string;
  model: string;
  status: TaskStatus;
  phase: TaskPhase | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  version: number;
  off_resolved: boolean | null;
  on_resolved: boolean | null;
  queue_position: number | null;
}

export interface TaskEvent {
  task_id: string;
  status: TaskStatus;
  phase: TaskPhase | null;
  version: number;
  occurred_at: string;
}

export interface Capabilities {
  benchmarks: "swebench-pro"[];
  instances: "instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9"[];
  models: "gpt-5.6-sol"[];
  reasoning_efforts: "medium"[];
  treatment_modes: "off_on"[];
}

export interface HealthResponse {
  service: "ok";
  worker_lease_active: boolean;
  queued_tasks: number;
  running_tasks: number;
}

export interface ArmResponse {
  arm: "off" | "on";
  resolution: "resolved" | "unresolved";
  input_tokens: number | null;
  output_tokens: number | null;
  elapsed_seconds: number | null;
  patch_bytes: number | null;
}

export interface MetricComparison {
  off: number;
  on: number;
  delta: number;
  percent: number | null;
}

export interface ComparisonResponse {
  input_tokens: MetricComparison | null;
  output_tokens: MetricComparison | null;
  elapsed_seconds: MetricComparison | null;
  patch_bytes: MetricComparison | null;
}

export interface TreatmentEvidence {
  mcp_requests: number;
  prompt_sources: number;
  plugin_checkout_sha: string;
  plugin_id: string;
  plugin_installed: boolean;
  plugin_version: string;
  scope_id: string;
  server_ready: boolean;
}

export interface EvidenceResponse {
  off: TreatmentEvidence;
  on: TreatmentEvidence;
}

export interface ReportResponse {
  task_id: string;
  acceptance_valid: boolean;
  off: ArmResponse & { arm: "off" };
  on: ArmResponse & { arm: "on" };
  comparison: ComparisonResponse;
  evidence: EvidenceResponse;
  revisions: Record<string, string>;
  configuration: Record<string, string>;
  generated_at: string;
}

export interface TaskListOptions {
  status?: TaskStatus;
  limit?: number;
  offset?: number;
}

export interface EventStreamError {
  code: "event_stream_disconnected" | "invalid_event";
  message: string;
  reconnecting: boolean;
}

export interface TaskEventSubscription {
  close(): void;
}
