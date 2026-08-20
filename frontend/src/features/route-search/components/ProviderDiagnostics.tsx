import { ProviderStatusResponse, RouteSearchResponse } from "../../../lib/api";
import { formatElapsedTime, formatStationList } from "../formatters";

interface ProviderDiagnosticsProps {
  providerStatus: ProviderStatusResponse | null;
  result: RouteSearchResponse | null;
  isLoading: boolean;
}

export function ProviderDiagnostics({
  providerStatus,
  result,
  isLoading,
}: ProviderDiagnosticsProps) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-3 text-sm text-zinc-600 shadow-sm">
      <div>
        中转候选算法：
        {providerStatus?.transfer_candidate_enabled
          ? "已启用真实数据源候选站生成"
          : "未启用真实数据源候选站生成（当前为 Mock 或状态未知）"}
      </div>
      <div>
        OD 请求预算：{providerStatus?.max_remote_queries ?? "-"}
        ；并发上限：{providerStatus?.max_concurrent_remote_queries ?? "-"}
        ；上次实际请求：{providerStatus?.last_remote_query_count ?? "-"}
        ；查询耗时：
        {result?.elapsed_ms !== undefined
          ? formatElapsedTime(result.elapsed_ms)
          : "-"}
      </div>
      <div>
        最终快照：已搜索 OD 目标 {result?.searched_count ?? "-"}
        ；待完成 {result?.pending_count ?? "-"}
        ；当前展示已发现最低价 Top {result?.plans.length ?? 0}
        {isLoading ? "（搜索完成后统一返回最低价方案）" : ""}
      </div>
      <div>
        缓存命中：内存{" "}
        {providerStatus?.last_diagnostics.memory_cache_hit_count ?? "-"}
        ；SQLite{" "}
        {providerStatus?.last_diagnostics.persistent_cache_hit_count ?? "-"}
        ；完整方案价格界剪枝{" "}
        {providerStatus?.last_diagnostics.pruned_by_complete_price_bound_count ?? "-"}
        ；Pareto 剪枝{" "}
        {providerStatus?.last_diagnostics.pruned_by_pareto_count ?? "-"}
      </div>
      <div>
        已扩展 OD 目标：
        {formatStationList(providerStatus?.last_diagnostics.expanded_od_targets)}
      </div>
      {providerStatus?.last_diagnostics.failed_od_targets.length ? (
        <div>
          失败 OD 目标：
          {formatStationList(providerStatus.last_diagnostics.failed_od_targets)}
        </div>
      ) : null}
    </section>
  );
}
