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
