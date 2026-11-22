import { useMemo, useState } from "react";
import { RouteSearchResponse } from "../../../lib/api";
import { RoutePlanCard } from "./RoutePlanCard";
import { PLAN_SORT_OPTIONS, PlanSortMode, sortPlans } from "../sortPlans";

interface RoutePlansProps {
  result: RouteSearchResponse | null;
}

export function RoutePlans({ result }: RoutePlansProps) {
  const [sortMode, setSortMode] = useState<PlanSortMode>("default");

  const plans = result?.plans ?? [];
  const sortedPlans = useMemo(() => sortPlans(plans, sortMode), [plans, sortMode]);

  return (
    <section className="grid gap-3">
      {plans.length > 0 ? (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-sm text-zinc-500">共 {plans.length} 个方案</div>
          <div
            className="flex flex-wrap gap-1"
            role="group"
            aria-label="结果排序方式"
          >
            {PLAN_SORT_OPTIONS.map((option) => {
              const active = option.value === sortMode;
              return (
                <button
                  key={option.value}
                  type="button"
                  title={option.hint}
                  aria-pressed={active}
                  onClick={() => setSortMode(option.value)}
                  className={
                    "rounded-full border px-3 py-1 text-xs transition-colors " +
                    (active
                      ? "border-zinc-900 bg-zinc-900 font-medium text-white"
                      : "border-zinc-200 bg-white text-zinc-600 hover:bg-zinc-100")
                  }
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        </div>
      ) : null}

      {result && result.plans.length === 0 ? (
        <div className="rounded-lg border border-zinc-200 bg-white p-8 text-center text-sm text-zinc-500">
          暂无可用方案
        </div>
      ) : null}

      {sortedPlans.map((plan, index) => (
        <RoutePlanCard
          key={planKey(plan)}
          plan={plan}
          updatedAt={result?.updated_at ?? ""}
          index={index}
        />
      ))}
    </section>
  );
}

// 同一批方案会被反复排序，用「车次 + 发车时刻」当 key 才能在换序时复用 DOM
// （用下标当 key 会让 React 错认卡片，动画和输入状态都会串）。
function planKey(plan: RouteSearchResponse["plans"][number]) {
  return plan.segments
    .map((segment) => `${segment.train_no}@${segment.depart_at}`)
    .join("|");
}
