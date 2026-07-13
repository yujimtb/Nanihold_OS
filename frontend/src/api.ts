import type { AppConfig, ChatResponse, ChatSession, RunDetail, RunSummary, SelfDevProposalDetail, SelfDevProposalSummary, Topology } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail || `Request failed: ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  config: () => request<AppConfig>("/api/config"),
  createChat: (backend: "claude-code" | "codex", model?: string) =>
    request<ChatSession>("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backend, model: model?.trim() || null }),
    }),
  getChat: (chatId: string) => request<ChatSession>(`/api/chat/${chatId}`),
  sendChatMessage: (chatId: string, text: string) =>
    request<ChatResponse>(`/api/chat/${chatId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),
  listRuns: () => request<RunSummary[]>("/api/runs"),
  getRun: (runId: string) => request<RunDetail>(`/api/runs/${runId}`),
  createRun: (goal: string) => request<RunDetail>("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal, constraints: {} }),
  }),
  topology: (runId: string) => request<Topology>(`/api/runs/${runId}/topology`),
  instruct: (runId: string, instruction: string, targetNode?: string) =>
    request<{ delivered: boolean }>(`/api/runs/${runId}/instructions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction, target_node: targetNode }),
    }),
  controlNode: (runId: string, nodeId: string, action: "suspend" | "resume" | "terminate") =>
    request<{ status: string }>(`/api/runs/${runId}/nodes/${nodeId}/control`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    }),
  algedonic: (runId: string, severity: "pain" | "pleasure", reason: string, sourceNodeId: string) =>
    request<{ delivered: boolean }>(`/api/runs/${runId}/algedonic`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ severity, reason, source_node_id: sourceNodeId }),
    }),
  consortiumStatement: (consortiumId: string, statement: string) =>
    request<{ accepted: boolean }>(`/api/consortium/${consortiumId}/statement`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ statement }),
    }),
  humanReview: (runId: string, reviewKey: string, response: string) =>
    request<{ accepted: boolean }>(`/api/runs/${runId}/human-review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review_key: reviewKey, response }),
    }),
  interrupt: (runId: string, instruction: string) =>
    request<RunDetail>(`/api/runs/${runId}/interrupt`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction }),
    }),
  cancel: (runId: string) =>
    request<RunDetail>(`/api/runs/${runId}/cancel`, { method: "POST" }),
  retry: (runId: string) =>
    request<RunDetail>(`/api/runs/${runId}/retry`, { method: "POST" }),
  usePartial: (runId: string) =>
    request<RunDetail>(`/api/runs/${runId}/use-partial`, { method: "POST" }),
  delete: (runId: string) =>
    request<void>(`/api/runs/${runId}`, { method: "DELETE" }),
  streamUrl: (runId: string) => `${API_BASE}/api/runs/${runId}/events`,
  attachmentUrl: (runId: string, attachmentId: string) =>
    `${API_BASE}/api/runs/${runId}/attachments/${attachmentId}`,
  artifactUrl: (runId: string, name: string) =>
    `${API_BASE}/api/runs/${runId}/artifacts/${encodeURIComponent(name)}`,
  selfdevList: (params?: { state?: string; pendingAction?: string }) => {
    const query = new URLSearchParams();
    if (params?.state) query.set("state", params.state);
    if (params?.pendingAction) query.set("pending_action", params.pendingAction);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return request<{ items: SelfDevProposalSummary[] }>(`/api/selfdev/proposals${suffix}`);
  },
  selfdevDetail: (proposalId: string) =>
    request<SelfDevProposalDetail>(`/api/selfdev/proposals/${encodeURIComponent(proposalId)}`),
  selfdevCreate: (payload: Record<string, unknown>) =>
    request<{ proposal_id: string; state: string; state_version: number; created_at: string }>("/api/selfdev/proposals", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  selfdevControl: (proposalId: string, action: "suspend" | "resume" | "abort" | "force_abort", reason: string, stateVersion: number, pauseId?: string) =>
    request<{ accepted: boolean }>(`/api/selfdev/proposals/${encodeURIComponent(proposalId)}/control`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, reason, expected_state_version: stateVersion, ...(pauseId ? { pause_id: pauseId } : {}) }),
    }),
  selfdevHumanDecision: (proposalId: string, payload: Record<string, unknown>) =>
    request<{ accepted: boolean }>(`/api/selfdev/proposals/${encodeURIComponent(proposalId)}/human-decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  selfdevMergeOutcome: (proposalId: string, merged: boolean, reason: string) =>
    request<{ accepted: boolean }>(`/api/selfdev/proposals/${encodeURIComponent(proposalId)}/merge-outcome`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ merged, reason }),
    }),
  selfdevArtifactUrl: (proposalId: string, name: string) =>
    `${API_BASE}/api/selfdev/proposals/${encodeURIComponent(proposalId)}/artifacts/${name.split("/").map(encodeURIComponent).join("/")}`,
};
