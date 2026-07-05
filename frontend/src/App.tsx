import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  Clock,
  Loader2,
  Search,
  Square,
  TrainFront,
} from "lucide-react";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import {
  ApiError,
  ProviderStatusResponse,
  RouteSearchRequest,
  RouteSearchResponse,
  getProviderStatus,
  searchRoutesStream,
} from "./lib/api";

const today = new Date().toISOString().slice(0, 10);
type CandidateStrategy = NonNullable<RouteSearchRequest["candidate_strategy"]>;

export function App() {
  const [fromStation, setFromStation] = useState("北京");
  const [toStation, setToStation] = useState("上海");
  const [date, setDate] = useState(today);
  const [minTransferMinutes, setMinTransferMinutes] = useState(30);
  const [maxTransfers, setMaxTransfers] = useState(1);
  const [candidateLimit, setCandidateLimit] = useState(50);
  const [candidateStrategy, setCandidateStrategy] =
    useState<CandidateStrategy>("balanced");
  const [maxDetourRatio, setMaxDetourRatio] = useState("");
  const [maxDetourKm, setMaxDetourKm] = useState("");
  const [corridorWidthKm, setCorridorWidthKm] = useState("");
  const [maxTotalDurationMinutes, setMaxTotalDurationMinutes] = useState("");
  const [result, setResult] = useState<RouteSearchResponse | null>(null);
  const [providerStatus, setProviderStatus] =
    useState<ProviderStatusResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let ignore = false;

    async function loadProviderStatus() {
      try {
        const status = await getProviderStatus();
        if (!ignore) {
          setProviderStatus(status);
        }
      } catch {
        if (!ignore) {
          setProviderStatus(null);
        }
      }
    }

    loadProviderStatus();
    return () => {
      ignore = true;
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    abortControllerRef.current?.abort();
    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    setIsLoading(true);
    setError(null);
    setResult(null);

    const payload: RouteSearchRequest = {
      from_station: fromStation,
      to_station: toStation,
      date,
      max_transfers: maxTransfers,
      min_transfer_minutes: minTransferMinutes,
      candidate_limit: candidateLimit,
      candidate_strategy: candidateStrategy,
      max_detour_ratio: parseOptionalNumber(maxDetourRatio),
      max_detour_km: parseOptionalNumber(maxDetourKm),
      corridor_width_km: parseOptionalNumber(corridorWidthKm),
      max_total_duration_minutes: parseOptionalNumber(maxTotalDurationMinutes),
    };

    try {
      const response = await searchRoutesStream(
        payload,
        (partialResponse) => setResult(partialResponse),
        abortController.signal,
      );
      setResult(response);
      setProviderStatus(await getProviderStatus());
    } catch (caught) {
      if (!isAbortError(caught)) {
        setError(formatSearchError(caught));
      }
    } finally {
      if (abortControllerRef.current === abortController) {
        abortControllerRef.current = null;
      }
      setIsLoading(false);
    }
  }

  function handleAbortSearch() {
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
    setIsLoading(false);
  }

  return (
    <main className="min-h-screen bg-zinc-50 text-zinc-950">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 py-6 md:px-6">
        <header className="flex flex-col gap-2 border-b border-zinc-200 pb-5 md:flex-row md:items-end md:justify-between">
          <div>
            <div className="flex items-center gap-2 text-sm text-zinc-500">
              <TrainFront className="h-4 w-4" />
              HardSeat Hero
            </div>
            <h1 className="mt-2 text-2xl font-semibold tracking-normal md:text-3xl">
              低价中转方案查询
            </h1>
          </div>
          <div className="text-sm text-zinc-500">
            数据源：{providerStatus?.provider ?? result?.source ?? "mock"}
          </div>
        </header>

        <section className="rounded-lg border border-zinc-200 bg-white p-3 text-sm text-zinc-600 shadow-sm">
          <div>
            中转候选算法：
            {providerStatus?.transfer_candidate_enabled
              ? "已启用真实数据源候选站生成"
              : "未启用真实数据源候选站生成（当前为 Mock 或状态未知）"}
          </div>
          <div>
            OD 请求预算：{providerStatus?.max_remote_queries ?? "-"}
            ；并发上限：
            {providerStatus?.max_concurrent_remote_queries ?? "-"}
            ；上次实际请求：
            {providerStatus?.last_remote_query_count ?? "-"}
            ；查询耗时：
            {result?.elapsed_ms !== undefined
              ? formatElapsedTime(result.elapsed_ms)
              : "-"}
          </div>
          <div>
            实时快照：已搜索候选 {result?.searched_count ?? "-"}
            ；待完成 {result?.pending_count ?? "-"}
            ；当前展示已发现最低价 Top {result?.plans.length ?? 0}
            {isLoading ? "（搜索推进时会自动刷新并移除被挤出的方案）" : ""}
          </div>
          <div>
            缓存命中：内存{" "}
            {providerStatus?.last_diagnostics.memory_cache_hit_count ?? "-"}
            ；SQLite{" "}
            {providerStatus?.last_diagnostics.persistent_cache_hit_count ?? "-"}
            ；价格剪枝{" "}
            {providerStatus?.last_diagnostics.pruned_by_best_price_count ?? "-"}
            ；Pareto 剪枝{" "}
            {providerStatus?.last_diagnostics.pruned_by_pareto_count ?? "-"}
          </div>
          <div>
            已扩展候选：
            {formatStationList(
              providerStatus?.last_diagnostics.expanded_candidates,
            )}
          </div>
          {providerStatus?.last_diagnostics.failed_candidates.length ? (
            <div>
              失败候选：
              {formatStationList(
                providerStatus.last_diagnostics.failed_candidates,
              )}
            </div>
          ) : null}
        </section>

        <section className="rounded-lg border border-zinc-200 bg-white p-4 shadow-sm">
          <form
            className="grid gap-4 md:grid-cols-[1fr_1fr_180px_170px_auto] md:items-end"
            onSubmit={handleSubmit}
          >
            <Field label="出发地">
              <Input
                value={fromStation}
                onChange={(event) => setFromStation(event.target.value)}
              />
            </Field>
            <Field label="目的地">
              <Input
                value={toStation}
                onChange={(event) => setToStation(event.target.value)}
              />
            </Field>
            <Field label="日期">
              <Input
                type="date"
                value={date}
                onChange={(event) => setDate(event.target.value)}
              />
            </Field>
            <Field label="最短换乘">
              <Input
                type="number"
                min={0}
                max={360}
                value={minTransferMinutes}
                onChange={(event) =>
                  setMinTransferMinutes(Number(event.target.value))
                }
              />
            </Field>
            <Button disabled={isLoading} type="submit">
              {isLoading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Search className="h-4 w-4" />
              )}
              查询
            </Button>
            {isLoading ? (
              <Button type="button" onClick={handleAbortSearch}>
                <Square className="h-4 w-4" />
                中断
              </Button>
            ) : null}
            <div className="grid gap-3 rounded-md border border-dashed border-zinc-200 bg-zinc-50 p-3 md:col-span-full md:grid-cols-4">
              <Field label="最大中转次数">
                <select
                  className="h-10 w-full rounded-md border border-zinc-300 bg-white px-3 text-sm outline-none transition focus:border-zinc-950"
                  value={maxTransfers}
                  onChange={(event) =>
                    setMaxTransfers(Number(event.target.value))
                  }
                >
                  <option value={0}>0 次（只查直达）</option>
                  <option value={1}>1 次</option>
                  <option value={2}>2 次</option>
                </select>
              </Field>
              <Field label="搜索策略">
                <select
                  className="h-10 w-full rounded-md border border-zinc-300 bg-white px-3 text-sm outline-none transition focus:border-zinc-950"
                  value={candidateStrategy}
                  onChange={(event) =>
                    setCandidateStrategy(
                      event.target.value as CandidateStrategy,
                    )
                  }
                >
                  <option value="balanced">标准</option>
                  <option value="direct_corridor">保守走廊</option>
                  <option value="wide_detour">允许绕行</option>
                  <option value="hub_first">枢纽优先</option>
                  <option value="exhaustive_budgeted">预算内尽量扩展</option>
                </select>
              </Field>
              <Field label="候选站上限">
                <Input
                  type="number"
                  min={1}
                  max={200}
                  value={candidateLimit}
                  onChange={(event) =>
                    setCandidateLimit(Number(event.target.value))
                  }
                />
              </Field>
              <Field label="最大总耗时（分钟，可空）">
                <Input
                  type="number"
                  min={60}
                  value={maxTotalDurationMinutes}
                  onChange={(event) =>
                    setMaxTotalDurationMinutes(event.target.value)
                  }
                  placeholder="不限制"
                />
              </Field>
              <Field label="最大绕行比例，可空">
                <Input
                  type="number"
                  min={0}
                  max={3}
                  step={0.1}
                  value={maxDetourRatio}
                  onChange={(event) => setMaxDetourRatio(event.target.value)}
                  placeholder="策略默认"
                />
              </Field>
              <Field label="最大绕行公里，可空">
                <Input
                  type="number"
                  min={0}
                  max={5000}
                  value={maxDetourKm}
                  onChange={(event) => setMaxDetourKm(event.target.value)}
                  placeholder="策略默认"
                />
              </Field>
              <Field label="走廊宽度 km，可空">
                <Input
                  type="number"
                  min={1}
                  max={2000}
                  value={corridorWidthKm}
                  onChange={(event) => setCorridorWidthKm(event.target.value)}
                  placeholder="策略默认"
                />
              </Field>
              <div className="text-xs leading-5 text-zinc-500 md:col-span-1 md:self-end">
                搜索越深，请求越多、耗时越长，但更不容易漏掉绕行中转方案。
              </div>
            </div>
          </form>
        </section>

        {error ? (
          <div className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {error}
          </div>
        ) : null}

        <section className="grid gap-3">
          {result && result.plans.length === 0 ? (
            <div className="rounded-lg border border-zinc-200 bg-white p-8 text-center text-sm text-zinc-500">
              暂无可用方案
            </div>
          ) : null}

          {result?.plans.map((plan, index) => (
            <article
              key={`${plan.total_price}-${index}`}
              className="rounded-lg border border-zinc-200 bg-white p-4 shadow-sm"
            >
              <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                <div>
                  <div className="text-xl font-semibold">
                    ¥{Number(plan.total_price).toFixed(1)}
                  </div>
                  <div className="mt-1 flex flex-wrap gap-3 text-sm text-zinc-500">
                    <span className="inline-flex items-center gap-1">
                      <Clock className="h-4 w-4" />
                      {formatDuration(plan.total_duration_minutes)}
                    </span>
                    <span>
                      {plan.transfer_stations.length
                        ? `经 ${plan.transfer_stations.join("、")}`
                        : "直达"}
                    </span>
                    <span>
                      换乘等待 {formatDuration(plan.transfer_minutes)}
                    </span>
                  </div>
                </div>
                <div className="text-sm text-zinc-500">
                  更新：{formatDateTime(result.updated_at)}
                </div>
              </div>

              <div className="mt-4 grid gap-2">
                {plan.segments.map((segment) => {
                  const baseTime =
                    plan.segments[0]?.depart_at ?? segment.depart_at;

                  return (
                    <div
                      key={`${segment.train_no}-${segment.depart_at}`}
                      className="grid gap-3 rounded-md bg-zinc-50 p-3 md:grid-cols-[120px_1fr_auto] md:items-center"
                    >
                      <div className="font-medium">{segment.train_no}</div>
                      <div className="flex items-center gap-2 text-sm">
                        <span>{segment.from_station}</span>
                        <span className="text-zinc-400">
                          {formatTimeWithDayOffset(segment.depart_at, baseTime)}
                        </span>
                        <ArrowRight className="h-4 w-4 text-zinc-400" />
                        <span>{segment.to_station}</span>
                        <span className="text-zinc-400">
                          {formatTimeWithDayOffset(segment.arrive_at, baseTime)}
                        </span>
                      </div>
                      <div className="flex flex-wrap gap-1.5 text-sm text-zinc-600 md:justify-end">
                        {segment.prices.length > 0
                          ? segment.prices.map((seatPrice) => {
                              const remaining = formatRemaining(
                                seatPrice.remaining,
                              );

                              return (
                                <span
                                  key={`${seatPrice.seat_type}-${seatPrice.price}`}
                                  className="rounded-full border border-zinc-200 bg-white px-2 py-1"
                                >
                                  {seatPrice.seat_type}{" "}
                                  {formatSeatPrice(seatPrice.price)}
                                  {remaining ? ` · ${remaining}` : ""}
                                </span>
                              );
                            })
                          : "暂无票价"}
                      </div>
                    </div>
                  );
                })}
              </div>
            </article>
          ))}
        </section>
      </div>
    </main>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="grid gap-1 text-sm font-medium text-zinc-700">
      {label}
      {children}
    </label>
  );
}

function formatDuration(minutes: number) {
  return `${Math.floor(minutes / 60)}小时${minutes % 60}分钟`;
}

function formatElapsedTime(milliseconds: number) {
  if (milliseconds < 1000) {
    return `${milliseconds} ms`;
  }
  return `${(milliseconds / 1000).toFixed(2)} 秒`;
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatTimeWithDayOffset(value: string, baseValue: string) {
  const valueDate = new Date(value);
  const baseDate = new Date(baseValue);
  const dayOffset = calendarDayOffset(valueDate, baseDate);

  if (dayOffset === 0) {
    return formatTime(value);
  }
  if (dayOffset === 1) {
    return `次日 ${formatTime(value)}`;
  }
  return `${formatMonthDay(valueDate)} ${formatTime(value)}`;
}

function calendarDayOffset(value: Date, base: Date) {
  const valueDay = new Date(
    value.getFullYear(),
    value.getMonth(),
    value.getDate(),
  );
  const baseDay = new Date(base.getFullYear(), base.getMonth(), base.getDate());
  return Math.round((valueDay.getTime() - baseDay.getTime()) / 86_400_000);
}

function formatMonthDay(value: Date) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
  }).format(value);
}

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatStationList(stations: string[] | undefined) {
  if (!stations || stations.length === 0) {
    return "-";
  }
  return stations.slice(0, 8).join("、") + (stations.length > 8 ? " 等" : "");
}

function parseOptionalNumber(value: string) {
  if (value.trim() === "") {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatSeatPrice(price: string) {
  return `¥${Number(price).toFixed(1)}`;
}

function formatRemaining(remaining: string) {
  return remaining && remaining !== "unknown" ? remaining : null;
}

function formatSearchError(caught: unknown) {
  if (caught instanceof ApiError) {
    if (caught.category === "data_source_unavailable") {
      return `数据源不可用：${caught.message}`;
    }
    if (caught.category === "bad_request") {
      return `查询参数错误：${caught.message}`;
    }
    return caught.message;
  }
  if (caught instanceof TypeError) {
    return "后端服务不可用，请确认 API 服务已启动。";
  }
  return caught instanceof Error ? caught.message : "查询失败";
}

function isAbortError(caught: unknown) {
  return caught instanceof DOMException && caught.name === "AbortError";
}
