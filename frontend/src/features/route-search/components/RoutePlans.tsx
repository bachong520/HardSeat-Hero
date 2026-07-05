import { RouteSearchResponse } from "../../../lib/api";
import { RoutePlanCard } from "./RoutePlanCard";

interface RoutePlansProps {
  result: RouteSearchResponse | null;
}

export function RoutePlans({ result }: RoutePlansProps) {
  return (
    <section className="grid gap-3">
      {result && result.plans.length === 0 ? (
        <div className="rounded-lg border border-zinc-200 bg-white p-8 text-center text-sm text-zinc-500">
          暂无可用方案
        </div>
      ) : null}

      {result?.plans.map((plan, index) => (
        <RoutePlanCard
          key={`${plan.total_price}-${index}`}
          plan={plan}
          updatedAt={result.updated_at}
          index={index}
        />
      ))}
    </section>
  );
}
