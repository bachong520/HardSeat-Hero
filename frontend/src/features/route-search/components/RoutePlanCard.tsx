import { Clock } from "lucide-react";
import { TransferPlan } from "../../../lib/api";
import { formatDateTime, formatDuration } from "../formatters";
import { TrainSegmentRow } from "./TrainSegmentRow";

interface RoutePlanCardProps {
  plan: TransferPlan;
  updatedAt: string;
  index: number;
}

export function RoutePlanCard({ plan, updatedAt, index }: RoutePlanCardProps) {
  return (
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
            <span>换乘等待 {formatDuration(plan.transfer_minutes)}</span>
          </div>
        </div>
        <div className="text-sm text-zinc-500">更新：{formatDateTime(updatedAt)}</div>
      </div>

      <div className="mt-4 grid gap-2">
        {plan.segments.map((segment) => (
          <TrainSegmentRow
            key={`${segment.train_no}-${segment.depart_at}`}
            segment={segment}
            baseTime={plan.segments[0]?.depart_at ?? segment.depart_at}
          />
        ))}
      </div>
    </article>
  );
}
