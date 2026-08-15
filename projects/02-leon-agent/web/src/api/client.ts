export interface HealthResponse {
  ok: boolean;
  service?: string;
  [key: string]: unknown;
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch("/api/health", { signal });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return (await response.json()) as HealthResponse;
}
