export type ApiRunSnapshot = {
  run_id: string;
  workflow_id: string | null;
  workflow_version: string | null;
  workflow_path: string | null;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  current_steps: string[];
  completed_steps: string[];
  failed_steps: string[];
  waiting_human_steps: string[];
  step_status: Record<
    string,
    {
      status: string;
      attempt: number;
      started_at: string | null;
      ended_at: string | null;
      artifacts: Record<string, string>;
      error: string | null;
    }
  >;
  error_count: number;
  human_intervention_count: number;
};

export type ApiRunSnapshotResponse = {
  snapshot_version: number;
  request_id: string;
  retrieved_at: string;
  run: ApiRunSnapshot;
};

export type ApiRunEvent = {
  timestamp: string;
  level: string;
  component: string;
  event: string;
  message: string;
  run_id: string;
  trace_id: string | null;
  request_id: string | null;
  workflow_id: string | null;
  workflow_path: string | null;
  step_id: string | null;
  duration_ms: number | null;
  error_code: string | null;
  error_message: string | null;
  data: Record<string, unknown>;
};

export type ApiRunEventsResponse = {
  snapshot_version: number;
  request_id: string;
  retrieved_at: string;
  run_id: string;
  count: number;
  events: ApiRunEvent[];
};

export type ApiRunArtifact = {
  key: string;
  path: string;
  original_path: string | null;
  step_id: string | null;
  size_bytes: number;
  registered_at: string | null;
};

export type ApiRunArtifactsResponse = {
  snapshot_version: number;
  request_id: string;
  retrieved_at: string;
  run_id: string;
  count: number;
  artifacts: ApiRunArtifact[];
};

type ApiErrorPayload = {
  error?: string;
  message?: string;
};

export async function fetchRunSnapshot(apiBaseUrl: string, runId: string): Promise<ApiRunSnapshotResponse> {
  return requestJson<ApiRunSnapshotResponse>(`${normalizeBaseUrl(apiBaseUrl)}/runs/${encodeURIComponent(runId)}`);
}

export async function fetchRunEvents(
  apiBaseUrl: string,
  runId: string,
  limit = 25,
): Promise<ApiRunEventsResponse> {
  return requestJson<ApiRunEventsResponse>(
    `${normalizeBaseUrl(apiBaseUrl)}/runs/${encodeURIComponent(runId)}/events?limit=${limit}`,
  );
}

export async function fetchRunArtifacts(
  apiBaseUrl: string,
  runId: string,
): Promise<ApiRunArtifactsResponse> {
  return requestJson<ApiRunArtifactsResponse>(
    `${normalizeBaseUrl(apiBaseUrl)}/runs/${encodeURIComponent(runId)}/artifacts`,
  );
}

async function requestJson<T>(url: string): Promise<T> {
  const response = await fetch(url, {
    headers: {
      Accept: "application/json",
    },
  });
  if (!response.ok) {
    const errorPayload = await tryParseError(response);
    throw new Error(errorPayload?.message ?? errorPayload?.error ?? `API request failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

async function tryParseError(response: Response): Promise<ApiErrorPayload | null> {
  try {
    return (await response.json()) as ApiErrorPayload;
  } catch {
    return null;
  }
}

function normalizeBaseUrl(value: string): string {
  return value.trim().replace(/\/+$/, "");
}
