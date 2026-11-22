import { TransferPlan } from "../../lib/api";

// 后端按 (总价, 换乘次数, 换乘等待, 总时长) 综合排序后只返回前 10 条，那是「默认视角」。
// 同一批结果换一个视角看，答案会不一样：最便宜的不一定最快，最快的不一定等车最少，
// 白天出发的也不一定比夜里出发的划算。
export type PlanSortMode = "default" | "price" | "duration" | "wait" | "earliest";

export const PLAN_SORT_OPTIONS: {
  value: PlanSortMode;
  label: string;
  hint: string;
}[] = [
  { value: "default", label: "默认", hint: "后端综合排序（价格优先，兼顾换乘次数与等待）" },
  { value: "price", label: "最低价", hint: "先比总价，同价再比总时长" },
  { value: "duration", label: "最快", hint: "先比总时长，同长再比总价" },
  { value: "wait", label: "少等待", hint: "先比换乘等待时间，少折腾" },
  { value: "earliest", label: "最早发车", hint: "按第一段的出发时刻从早到晚" },
];

export function sortPlans(
  plans: TransferPlan[],
  mode: PlanSortMode,
): TransferPlan[] {
  // 拷贝一份再排，避免就地修改 React state 里的数组。
  const sorted = [...plans];

  switch (mode) {
    case "price":
      sorted.sort(
        (left, right) =>
          comparePrice(left, right) ||
          left.total_duration_minutes - right.total_duration_minutes ||
          compareTransferCount(left, right),
      );
      break;
    case "duration":
      sorted.sort(
        (left, right) =>
          left.total_duration_minutes - right.total_duration_minutes ||
          comparePrice(left, right) ||
          compareTransferCount(left, right),
      );
      break;
    case "wait":
      sorted.sort(
        (left, right) =>
          left.transfer_minutes - right.transfer_minutes ||
          comparePrice(left, right) ||
          left.total_duration_minutes - right.total_duration_minutes,
      );
      break;
    case "earliest":
      sorted.sort(
        (left, right) =>
          compareFirstDeparture(left, right) ||
          comparePrice(left, right) ||
          left.total_duration_minutes - right.total_duration_minutes,
      );
      break;
    case "default":
    default:
      // 保持后端返回的顺序，不动。
      break;
  }

  return sorted;
}

function comparePrice(left: TransferPlan, right: TransferPlan) {
  return Number(left.total_price) - Number(right.total_price);
}

function compareTransferCount(left: TransferPlan, right: TransferPlan) {
  return left.transfer_stations.length - right.transfer_stations.length;
}

function compareFirstDeparture(left: TransferPlan, right: TransferPlan) {
  return firstDepartureTime(left) - firstDepartureTime(right);
}

function firstDepartureTime(plan: TransferPlan) {
  const first = plan.segments[0];
  if (!first) {
    return Number.POSITIVE_INFINITY;
  }
  const timestamp = new Date(first.depart_at).getTime();
  return Number.isNaN(timestamp) ? Number.POSITIVE_INFINITY : timestamp;
}
