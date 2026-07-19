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
  interface_node_id: string;
  status: string;
  provider_session_id: string | null;
};

export type Message = {
  message_id: string;
  conversation_id: string;
  role: "owner" | "interface";
  display_text: string | null;
  occurred_at: string;
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
