import { ArrowRight } from "lucide-react";
import { TrainSegment } from "../../../lib/api";
import {
  formatRemaining,
  formatSeatPrice,
  formatTimeWithDayOffset,
} from "../formatters";

interface TrainSegmentRowProps {
  segment: TrainSegment;
  baseTime: string;
}

export function TrainSegmentRow({ segment, baseTime }: TrainSegmentRowProps) {
  return (
    <div className="grid gap-3 rounded-md bg-zinc-50 p-3 md:grid-cols-[120px_1fr_auto] md:items-center">
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
              const remaining = formatRemaining(seatPrice.remaining);

              return (
                <span
                  key={`${seatPrice.seat_type}-${seatPrice.price}`}
                  className="rounded-full border border-zinc-200 bg-white px-2 py-1"
                >
                  {seatPrice.seat_type} {formatSeatPrice(seatPrice.price)}
                  {remaining ? ` · ${remaining}` : ""}
                </span>
              );
            })
          : "暂无票价"}
      </div>
    </div>
  );
}
