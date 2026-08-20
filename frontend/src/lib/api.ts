const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export interface RouteSearchRequest {
  from_station: string;
  to_station: string;
  date: string;
  max_transfers: number;
  min_transfer_minutes: number;
  max_total_duration_minutes?: number | null;
  candidate_limit?: number;
  candidate_strategy?:
    | "balanced"
    | "direct_corridor"
    | "wide_detour"
    | "hub_first"
    | "exhaustive_budgeted";
  max_detour_ratio?: number | null;
  max_detour_km?: number | null;
  corridor_width_km?: number | null;
}

export interface TrainSegment {
  train_no: string;
  from_station: string;
  to_station: string;
  depart_at: string;
  arrive_at: string;
  duration_minutes: number;
  prices: Array<{ seat_type: string; price: string; remaining: string }>;
  source: string;
  updated_at: string;
}

export interface TransferPlan {
  total_price: string;
  total_duration_minutes: number;
  transfer_minutes: number;
  transfer_stations: string[];
  segments: TrainSegment[];
}

export interface RouteSearchResponse {
  query_id: string;
  source: string;
  updated_at: string;
  elapsed_ms?: number;
  searched_count?: number;
  pending_count?: number;
  remote_query_count?: number;
  final?: boolean;
  plans: TransferPlan[];
}

interface RouteSearchSnapshot {
  type: "snapshot";
  query_id: string;
  source: string;
  updated_at: string;
  elapsed_ms?: number;
  searched_count: number;
  pending_count: number;
  remote_query_count: number;
  final?: boolean;
  plans: TransferPlan[];
}

interface RouteSearchMetadata {
  type: "metadata";
  query_id: string;
  source: string;
  updated_at: string;
  elapsed_ms: number;
}

export interface ProviderStatusResponse {
  provider: string;
  search_engine: string;
  expansion_strategy?: string;
  online_remote_io: boolean;
  global_optimum_guaranteed?: boolean;
  status: string;
  transfer_candidate_enabled: boolean;
  max_remote_queries: number;
  max_concurrent_remote_queries: number;
  last_remote_query_count: number;
  last_diagnostics: SearchDiagnostics;
  updated_at: string;
}

export interface SearchDiagnostics {
  remote_query_count: number;
  memory_cache_hit_count: number;
  persistent_cache_hit_count: number;
  expanded_od_targets: string[];
  failed_od_targets: string[];
  pruned_by_complete_price_bound_count: number;
  pruned_by_pareto_count: number;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number | null,
    public readonly category:
      | "backend_unavailable"
      | "data_source_unavailable"
      | "bad_request"
      | "unknown",
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function searchRoutesStream(
  payload: RouteSearchRequest,
  onUpdate: (response: RouteSearchResponse) => void,
  signal?: AbortSignal,
): Promise<RouteSearchResponse> {
  const response = await fetch(`${API_BASE_URL}/api/routes/search/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });

  if (!response.ok) {
    throw await buildApiError(response, "查询失败");
  }
  if (!response.body) {
    throw new ApiError("浏览器不支持流式读取响应。", null, "unknown");
  }

  const result: RouteSearchResponse = {
    query_id: `${payload.date}:${payload.from_station}:${payload.to_station}`,
    source: "stream",
    updated_at: new Date().toISOString(),
    plans: [],
  };
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += value;
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      consumeStreamLine(line, result, onUpdate);
    }
  }
  if (buffer.trim()) {
    consumeStreamLine(buffer, result, onUpdate);
  }

  return result;
}

export async function getProviderStatus(): Promise<ProviderStatusResponse> {
  const response = await fetch(`${API_BASE_URL}/api/providers/status`);

  if (!response.ok) {
    throw await buildApiError(response, "数据源状态查询失败");
  }

  return response.json();
}

async function buildApiError(
  response: Response,
  fallbackMessage: string,
): Promise<ApiError> {
  const detail = await readErrorDetail(response);
  if (response.status === 422) {
    return new ApiError(
      detail ?? "请求参数不符合要求，请检查站点、日期和换乘时间。",
      response.status,
      "bad_request",
    );
  }
  if (
    response.status === 502 ||
    response.status === 503 ||
    response.status === 504
  ) {
    return new ApiError(
      detail ?? "数据源暂不可用，请稍后重试或切回 Mock 数据源。",
      response.status,
      "data_source_unavailable",
    );
  }
  return new ApiError(
    detail ?? `${fallbackMessage}：${response.status}`,
    response.status,
    "unknown",
  );
}

function consumeStreamLine(
  line: string,
  result: RouteSearchResponse,
  onUpdate: (response: RouteSearchResponse) => void,
) {
  if (!line.trim()) {
    return;
  }
  const parsed = JSON.parse(line) as
    | TransferPlan
    | RouteSearchSnapshot
    | RouteSearchMetadata
    | { error?: string };
  if ("error" in parsed && parsed.error) {
    throw new ApiError(parsed.error, 502, "data_source_unavailable");
  }
  if ("type" in parsed && parsed.type === "snapshot") {
    result.query_id = parsed.query_id;
    result.source = parsed.source;
    result.updated_at = parsed.updated_at;
    result.elapsed_ms = parsed.elapsed_ms;
    result.searched_count = parsed.searched_count;
    result.pending_count = parsed.pending_count;
    result.remote_query_count = parsed.remote_query_count;
    result.final = parsed.final;
    result.plans = parsed.plans;
    onUpdate({ ...result, plans: result.plans });
    return;
  }
  if ("type" in parsed && parsed.type === "metadata") {
    result.query_id = parsed.query_id;
    result.source = parsed.source;
    result.updated_at = parsed.updated_at;
    result.elapsed_ms = parsed.elapsed_ms;
    return;
  }
  const plan = parsed as TransferPlan;
  result.plans = [...result.plans, plan]
    .sort((left, right) => comparePlans(left, right))
    .slice(0, 20);
  onUpdate({ ...result, plans: result.plans });
}

function comparePlans(left: TransferPlan, right: TransferPlan) {
  const priceDiff = Number(left.total_price) - Number(right.total_price);
  if (priceDiff !== 0) {
    return priceDiff;
  }
  const durationDiff =
    left.total_duration_minutes - right.total_duration_minutes;
  if (durationDiff !== 0) {
    return durationDiff;
  }
  return left.transfer_stations.length - right.transfer_stations.length;
}

async function readErrorDetail(response: Response): Promise<string | null> {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    return null;
  }
  return null;
}
