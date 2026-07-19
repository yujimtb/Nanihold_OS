const API_BASE = import.meta.env.VITE_API_BASE_URL;
if (!API_BASE) {
  throw new Error("VITE_API_BASE_URL is required");
}

const CONFIGURED_DEVICE_ID = import.meta.env.VITE_NANIHOLD_DEVICE_ID;
if (CONFIGURED_DEVICE_ID !== undefined && !CONFIGURED_DEVICE_ID.trim()) {
  throw new Error("VITE_NANIHOLD_DEVICE_ID must not be blank");
}

const BROWSER_DEVICE_KEY = "nanihold-browser-device-id";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export function browserDeviceId(): string {
  if (CONFIGURED_DEVICE_ID) return CONFIGURED_DEVICE_ID;
  const existing = localStorage.getItem(BROWSER_DEVICE_KEY);
  if (existing) return existing;
  const created = `browser:${crypto.randomUUID()}`;
  localStorage.setItem(BROWSER_DEVICE_KEY, created);
  return created;
}

export class ApiClient {
  constructor(private readonly deviceId?: string) {}

  async get<T>(path: string): Promise<T> {
    return this.request<T>(path, { method: "GET" });
  }

  async post<T>(path: string, body: object): Promise<T> {
    return this.request<T>(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  private async request<T>(path: string, init: RequestInit): Promise<T> {
    const headers = new Headers(init.headers);
    if (this.deviceId) {
      headers.set("X-Nanihold-Device-Id", this.deviceId);
    }
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      credentials: "include",
      headers,
    });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        // An invalid error body is represented by status only.
      }
      throw new ApiError(response.status, `${response.status} ${detail}`);
    }
    return response.json() as Promise<T>;
  }
}
