export type RunStatus =
  | "queued"
  | "running"
  | "interrupting"
  | "waiting_for_user"
  | "completed"
  | "cancelled"
  | "failed";

export interface RunSummary {
  run_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  status: RunStatus;
  current_stage: string;
  progress: number;
  generation: number;
  runtimes: Array<{
    role: string;
    backend: string;
    model: string;
  }>;
}

export interface TimelineItem {
  id: string;
  generation: number;
  seq: number;
  ts?: string;
  type: string;
  stage: string;
  progress: number;
  system: string;
  title: string;
  summary: string;
  details: Record<string, unknown>;
  superseded: boolean;
}

export interface Attachment {
  attachment_id: string;
  name: string;
  media_type: string;
  size: number;
  has_text: boolean;
}

export interface RunDetail extends RunSummary {
  description: string;
  final_answer?: string | null;
  error?: string | null;
  attachments: Attachment[];
  artifacts: Array<{
    name: string;
    size: number;
    media_type: string;
  }>;
  timeline: TimelineItem[];
  generations: Array<{
    generation: number;
    runtime_run_id: string;
    instruction: string;
    started_at: string;
    status: string;
    finished_at?: string | null;
  }>;
}

export interface AppConfig {
  runtimes: Array<{
    role: string;
    backend: string;
    model: string;
  }>;
  demo_mode: boolean;
  single_run: boolean;
}

export type ChatRole = "user" | "assistant";

export interface ChatMessage {
  message_id: string;
  role: ChatRole;
  text: string;
  tokens: number;
  tokens_in: number;
  tokens_out: number;
  tokens_cache_read: number;
  latency_ms: number;
  created_at: string;
}

export interface ChatSession {
  chat_id: string;
  backend: "claude-code" | "codex";
  model: string | null;
  workdir: string;
  session_ref: string | null;
  messages: ChatMessage[];
  total_tokens: number;
}

export interface ChatResponse {
  chat_id: string;
  text: string;
  tokens: number;
  latency: number;
  latency_ms: number;
  tokens_in: number;
  tokens_out: number;
  tokens_cache_read: number;
  session_ref: string | null;
  message: ChatMessage;
}

export type NodeStatus = "CREATED" | "RUNNING" | "IDLE" | "SUSPENDED" | "WAITING" | "COMPLETED" | "TERMINATED" | "FAILED";

export interface TopologyNode {
  node_id: string;
  parent_id: string | null;
  role: string;
  status: NodeStatus;
  terminable: boolean;
  backend: string;
  model: string;
  activity: string;
  last_activity_at: string | null;
  recent_events: Array<{
    event_id?: string;
    seq?: number;
    ts?: string;
    event_type: string;
    summary: string;
    actor_type?: string;
    actor_id?: string | null;
  }>;
  authority: { kind: string; id?: string; summary: string; source?: string };
  budget: {
    tokens_limit: number;
    tokens_consumed: number;
    wall_clock_seconds_limit: number;
    wall_clock_seconds_consumed: number;
  };
}

export interface Topology {
  run_id: string;
  nodes: TopologyNode[];
  pending_human_reviews: Array<{ review_key: string; reason: string; subject: string }>;
  waiting_consortiums: Array<{ consortium_id: string; subject?: string }>;
}

export type ProposalState =
  | "PROPOSED" | "CONSORTIUM_REVIEW" | "APPROVED" | "REJECTED" | "NEEDS_HUMAN"
  | "WORKSPACE_READY" | "IMPLEMENTING" | "GATES_RUNNING" | "GATES_PASSED"
  | "GATES_FAILED" | "ABORTED" | "AUDIT" | "FINAL_CONSORTIUM" | "MERGE_READY"
  | "REJECTED_FINAL" | "DONE" | "ARCHIVED";

export interface SelfDevPauseCause {
  pause_id: string;
  kind: "SUSPEND" | "QUOTA_WAIT";
  actor_type: string;
  actor_id: string;
  pool_id: string | null;
  reset_at: string | null;
  source_event_id: string;
  reason: string;
}

export interface SelfDevProposalSummary {
  proposal_id: string;
  title: string;
  state: ProposalState;
  pause_causes: SelfDevPauseCause[];
  state_version: number;
  risk_class: "low" | "normal" | "protected";
  active_run_id: string | null;
  pending_action: string | null;
  updated_at: string;
}

export interface SelfDevProposalDetail extends SelfDevProposalSummary {
  schema_version: number;
  proposal: Record<string, unknown> & {
    id: string;
    title: string;
    motivation: string;
    scope: Array<{ path: string; kind: string }>;
    acceptance_criteria: Array<{ id: string; statement: string; verifier: Record<string, unknown> }>;
    risk_class: "low" | "normal" | "protected";
    budget_estimate: Record<string, unknown>;
  };
  proposal_manifest_sha256?: string;
  protected_scope_sha256?: string;
  transitions: Array<{ event_id: string; ts: string; transition: Record<string, unknown> }>;
  consortium_reviews: Array<Record<string, unknown>>;
  implementation_runs: Array<Record<string, unknown>>;
  gate_attempts: Array<Record<string, unknown>>;
  audit_report: Record<string, unknown> | null;
  budget_actual: Record<string, unknown>;
  artifacts: Array<{ name: string; kind: string; sha256: string; event_id: string; created_at: string }>;
  candidate: { branch?: string; commit_sha?: string; base_sha?: string; diff_sha256?: string } | null;
  pr_description: string | null;
  last_error: string | null;
}
