import type { AppConfig, RunDetail, RunSummary } from "./types";

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
  listRuns: () => request<RunSummary[]>("/api/runs"),
  getRun: (runId: string) => request<RunDetail>(`/api/runs/${runId}`),
  createRun: (description: string, files: File[]) => {
    const body = new FormData();
    body.set("description", description);
    files.forEach((file) => body.append("files", file));
    return request<RunDetail>("/api/runs", { method: "POST", body });
  },
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
};
