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
  model: string;
  demo_mode: boolean;
  single_run: boolean;
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
  authority: { kind: string; id?: string; summary: string };
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
