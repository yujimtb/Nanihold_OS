export type ActivationState =
  | "UNCOMMISSIONED"
  | "HISTORY_IMPORTED"
  | "REORIENTATION_ONLY"
  | "AWAITING_OWNER_CONFIRMATION"
  | "ACTIVE";

export type DataSpace = {
  data_space_id: string;
  owner_id: string;
  kind: string;
  lethe_location: string;
};

export type Node = {
  node_id: string;
  name: string;
  kind: string;
  parent_node_id: string | null;
  status: string;
  resident_functions: string[];
  resident_s3_parent_function: "S5";
};

export type WorkItem = {
  work_item_id: string;
  title: string;
  delegated_to_node_id: string;
  integration_owner_node_id: string;
  state: string;
  acceptance_criteria: string[];
  completion_evidence: Record<string, unknown> | null;
};

export type WorkEdge = {
  source_work_item_id: string;
  target_work_item_id: string;
  kind: string;
};

export type Execution = {
  execution_id: string;
  work_item_id: string;
  pilot_id: string;
  model_candidate_key: string;
  state: string;
  pause_reason: string | null;
};

export type EventItem = {
  cursor: number;
  event: {
    event_id: string;
    event_type: string;
    occurred_at: string;
    stream_id: string;
    actor_type: string;
    payload: Record<string, unknown>;
  };
};

export type Conversation = {
  conversation_id: string;
  data_space_id: string;
  interface_node_id: string;
  owner_id: string;
  title: string;
};

export type SurfaceBinding = {
  binding_id: string;
  conversation_id: string;
  surface:
    | "web"
    | "tui"
    | "claude_native"
    | "slack"
    | "discord"
    | "intercom"
    | "codex";
  source_session_id: string;
  channel_id: string;
  device_id: string;
};

export type PilotSession = {
  pilot_session_id: string;
  conversation_id: string;
  pilot_id: string;
  provider_session_id: string;
  last_event_cursor: number;
};

export type Message = {
  message_id: string;
  conversation_id: string;
  role: "owner" | "interface";
  display_text: string | null;
  occurred_at: string;
};

export type Commitment = {
  commitment_id: string;
  conversation_id: string;
  statement: string;
  work_item_id: string | null;
  state: "open" | "satisfied" | "withdrawn";
};

export type Decision = {
  decision_id: string;
  conversation_id: string;
  statement: string;
  supersedes_decision_id: string | null;
};

export type ModelCandidate = {
  key: string;
  adapter: string;
  adapter_version: string;
  provider: string;
  model_snapshot: string;
  effort: string;
  environment_fingerprint: string;
};

export type EvidenceCitation = {
  claim_ref: string;
  evidence_ref: string;
};

export type ReorientationAssessment = {
  assessment_id: string;
  import_id: string;
  conversation_id: string;
  generated_at: string;
  understanding: string;
  active_missions: string[];
  decisions_and_constraints: string[];
  open_commitment_ids: string[];
  unknowns: string[];
  resume_work_item_ids: string[];
  covered_session_index_ref: string;
  covered_session_count: number;
  history_cursor: number;
  current_state_cursor: number;
  citations: EvidenceCitation[];
};

export type HistoryImportReceipt = {
  schema: "schema:history-activation-handoff";
  schema_version: "1.0.0";
  inventory_id: string;
  data_space_id: string;
  manifest_digest: string;
  record_count: number;
  raw_bytes: number;
  cross_source_overlap_identities: number;
  sources: Array<{
    source_id: string;
    source_kind:
      | "claude_code"
      | "claude_ai"
      | "codex"
      | "intercom"
      | "lethe"
      | "nanihold_legacy"
      | "system_snapshot";
    ownership: "personal";
    owner_id: string;
    record_count: number;
    raw_bytes: number;
    digest_sha256: string;
    cutover_cursor: string;
  }>;
  session_count: number;
  sessions: Array<{
    session_ref: string;
    source_session_id: string;
    source_kind:
      | "claude_code"
      | "claude_ai"
      | "codex"
      | "intercom"
      | "lethe"
      | "nanihold_legacy"
      | "system_snapshot";
    source_id: string;
    message_count: number;
    first_message_at: string;
    last_message_at: string;
  }>;
  session_index_ref: string;
  open_commitments_ref: string;
  current_state_ref: string;
};

export type ActivationStatus = {
  state: ActivationState;
  import_receipt: HistoryImportReceipt | null;
  assessment: ReorientationAssessment | null;
  approved_at: string | null;
  status_model_calls: 0;
  reorientation_pilot_calls: number;
  reorientation_input_tokens: number;
  reorientation_output_tokens: number;
  reorientation_error: string | null;
  work_graph_snapshot_id: string | null;
  reorientation_conversation_id: string | null;
  reorientation_attempt_in_progress: boolean;
  pending_reorientation_revision_reason:
    | "missing_resume_work_item"
    | "owner_correction"
    | null;
};

export type ConversationActionReceipt = {
  action_id: string;
  conversation_id: string;
  status: "accepted" | "completed" | "failed";
  owner_message_id: string;
  interface_message: Message | null;
  event_cursor: number;
  error: string | null;
};
