export interface HealthResponse {
  ok: boolean;
  service?: string;
  [key: string]: unknown;
}

export interface SessionResponse {
  session_id: string;
  messages: SessionMessage[];
  active_turn: { retry: boolean } | null;
}

export interface SessionMessage {
  id?: number;
  role: string;
  content: string;
  created_at?: number;
  revisions?: Array<{ content: string; created_at?: number }>;
}

export interface CreateSessionResponse {
  session_id: string;
  created_at: number;
}

export interface MessageResponse {
  session_id: string;
  answer: string;
  ok: boolean;
}

export interface CancelResponse {
  session_id: string;
  cancelled: boolean;
}

export interface ImageStateResponse {
  tasks: Array<Record<string, unknown>>;
  images: Array<Record<string, unknown>>;
  errors: Record<string, string>;
}

export interface ImageMode {
  id: string;
  name: string;
  aliases?: string[];
}

export interface ImageModesResponse {
  default_mode_id: string;
  default_mode_name: string;
  modes: ImageMode[];
}

export interface ModelSettingsResponse {
  provider: string;
  provider_scope: string;
  base_url: string;
  default_model: string;
  selected_model: string | null;
  active_model: string;
  models: string[];
  catalog_error: string | null;
}

export interface VoiceCatalogResponse {
  enabled: boolean;
  default_voice_id: string;
  models: Array<Record<string, unknown>>;
  voices: Array<{
    id: string;
    name?: string | null;
    model?: string | null;
    languages?: string[];
    demo?: string | null;
  }>;
}

export interface LeonEvent {
  event: string;
  session_id: string;
  timestamp?: string;
  data: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, detail: string) {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
  }
}

const TOKEN_KEY = "leon_token";
const SESSION_KEY = "leon_session";

function readStorage(key: string): string {
  return typeof localStorage === "undefined" ? "" : localStorage.getItem(key) || "";
}

export class LeonApi {
  token = readStorage(TOKEN_KEY);
  sessionId = readStorage(SESSION_KEY);
  private readonly modelSettingsCache = new Map<string, ModelSettingsResponse>();

  setToken(token: string): void {
    this.token = token.trim();
    if (typeof localStorage !== "undefined") {
      if (this.token) localStorage.setItem(TOKEN_KEY, this.token);
      else localStorage.removeItem(TOKEN_KEY);
    }
  }

  setSession(sessionId: string): void {
    this.sessionId = sessionId;
    if (typeof localStorage !== "undefined") {
      localStorage.setItem(SESSION_KEY, sessionId);
    }
  }

  clearSession(): void {
    if (this.sessionId) this.modelSettingsCache.delete(this.sessionId);
    this.sessionId = "";
    if (typeof localStorage !== "undefined") localStorage.removeItem(SESSION_KEY);
  }

  logout(): void {
    this.setToken("");
    this.clearSession();
  }

  async checkHealth(signal?: AbortSignal, token = this.token): Promise<HealthResponse> {
    const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
    const response = await fetch("/api/health", { headers, signal });
    if (!response.ok) throw new ApiError(response.status, await response.text());
    return (await response.json()) as HealthResponse;
  }

  async getSession(sessionId: string): Promise<SessionResponse> {
    return this.request<SessionResponse>(`/api/agent/sessions/${sessionId}`);
  }

  async createSession(): Promise<CreateSessionResponse> {
    return this.request<CreateSessionResponse>("/api/agent/sessions", { method: "POST" });
  }

  async sendMessage(
    sessionId: string,
    content: string,
    options: { signal?: AbortSignal; retry?: boolean } = {},
  ): Promise<MessageResponse> {
    return this.request<MessageResponse>(`/api/agent/sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, retry: Boolean(options.retry) }),
      signal: options.signal,
    });
  }

  async cancelMessage(sessionId: string): Promise<CancelResponse> {
    const path = `/api/agent/sessions/${sessionId}/cancel`;
    try {
      return await this.request<CancelResponse>(path, { method: "POST" });
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 405) throw error;
      return this.request<CancelResponse>(path, { method: "DELETE" });
    }
  }

  async getImageState(sessionId: string, limit = 100): Promise<ImageStateResponse> {
    const safeLimit = Math.min(100, Math.max(1, Math.trunc(limit)));
    return this.request<ImageStateResponse>(
      `/api/agent/sessions/${sessionId}/image-state?limit=${safeLimit}`,
    );
  }

  async getImageModes(signal?: AbortSignal): Promise<ImageModesResponse> {
    return this.request<ImageModesResponse>("/api/image-modes", { signal });
  }

  async getModelSettings(sessionId: string, refresh = false): Promise<ModelSettingsResponse> {
    const cached = this.modelSettingsCache.get(sessionId);
    if (cached && !refresh) return cached;
    const suffix = refresh ? "?refresh=true" : "";
    const settings = await this.request<ModelSettingsResponse>(
      `/api/agent/sessions/${sessionId}/model${suffix}`,
    );
    this.modelSettingsCache.set(sessionId, settings);
    return settings;
  }

  getCachedModelSettings(sessionId: string): ModelSettingsResponse | null {
    return this.modelSettingsCache.get(sessionId) || null;
  }

  async setModelSettings(
    sessionId: string,
    model: string | null,
  ): Promise<ModelSettingsResponse> {
    const settings = await this.request<ModelSettingsResponse>(`/api/agent/sessions/${sessionId}/model`, {
      method: "PUT",
      body: JSON.stringify({ model }),
    });
    this.modelSettingsCache.set(sessionId, settings);
    return settings;
  }

  async getVoiceCatalog(refresh = false): Promise<VoiceCatalogResponse> {
    const suffix = refresh ? "?refresh=true" : "";
    return this.request<VoiceCatalogResponse>(`/api/voice/catalog${suffix}`);
  }

  async synthesizeSpeech(
    text: string,
    voiceId?: string,
    signal?: AbortSignal,
  ): Promise<Blob> {
    const headers = new Headers({ "Content-Type": "application/json" });
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    const response = await fetch("/api/agent/tts", {
      method: "POST",
      headers,
      body: JSON.stringify({ text, voice_id: voiceId || undefined }),
      signal,
    });
    if (response.status === 401) {
      this.logout();
      throw new ApiError(401, "Token 已失效，请重新登录");
    }
    if (!response.ok) {
      const raw = await response.text();
      let detail = raw;
      try {
        const payload = JSON.parse(raw) as { detail?: string; error?: string };
        detail = payload.detail || payload.error || raw;
      } catch {
        // A proxy may return plain text or HTML instead of JSON.
      }
      throw new ApiError(response.status, detail.replace(/<[^>]+>/g, " ").trim());
    }
    return response.blob();
  }

  connectEvents(
    sessionId: string,
    onEvent: (event: LeonEvent) => void,
    onError: (source: EventSource) => void,
  ): EventSource {
    const query = this.token ? `?token=${encodeURIComponent(this.token)}` : "";
    const source = new EventSource(`/api/agent/sessions/${sessionId}/events${query}`);
    source.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data) as LeonEvent);
      } catch {
        // Ignore malformed heartbeats or an incomplete event payload.
      }
    };
    source.onerror = () => onError(source);
    return source;
  }

  async getAsrStatus(signal?: AbortSignal): Promise<{ enabled: boolean }> {
    return this.request<{ enabled: boolean }>("/api/agent/asr/status", { signal });
  }

  async transcribeAudio(blob: Blob, signal?: AbortSignal): Promise<string> {
    const headers = new Headers();
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    const form = new FormData();
    const type = blob.type || "audio/webm";
    const extension = type.includes("mp4") ? "mp4" : "webm";
    form.append("audio", blob, `speech.${extension}`);
    const response = await fetch("/api/agent/asr", {
      method: "POST",
      headers,
      body: form,
      signal,
    });
    if (response.status === 401) {
      this.logout();
      throw new ApiError(401, "Token 已失效，请重新登录");
    }
    if (!response.ok) {
      let detail = "";
      try {
        const payload = (await response.json()) as { detail?: string };
        detail = payload.detail || "";
      } catch {
        detail = await response.text();
      }
      throw new ApiError(response.status, detail);
    }
    const payload = (await response.json()) as { text?: string };
    return (payload.text || "").trim();
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Content-Type", "application/json");
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    const response = await fetch(path, { ...init, headers });
    if (response.status === 401) {
      this.logout();
      throw new ApiError(401, "Token 已失效，请重新登录");
    }
    if (!response.ok) {
      let detail = "";
      try {
        const payload = (await response.json()) as { detail?: string };
        detail = payload.detail || "";
      } catch {
        detail = await response.text();
      }
      throw new ApiError(response.status, detail);
    }
    return (await response.json()) as T;
  }
}

export const api = new LeonApi();

// Kept as a small function for components/tests that only need liveness.
export function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return api.checkHealth(signal);
}
