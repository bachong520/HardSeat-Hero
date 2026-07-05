import { ApiError } from "../../lib/api";

export function formatDuration(minutes: number) {
  return `${Math.floor(minutes / 60)}小时${minutes % 60}分钟`;
}

export function formatElapsedTime(milliseconds: number) {
  if (milliseconds < 1000) {
    return `${milliseconds} ms`;
  }
  return `${(milliseconds / 1000).toFixed(2)} 秒`;
}

export function formatTimeWithDayOffset(value: string, baseValue: string) {
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

export function formatDateTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

export function formatStationList(stations: string[] | undefined) {
  if (!stations || stations.length === 0) {
    return "-";
  }
  return stations.slice(0, 8).join("、") + (stations.length > 8 ? " 等" : "");
}

export function parseOptionalNumber(value: string) {
  if (value.trim() === "") {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function formatSeatPrice(price: string) {
  return `¥${Number(price).toFixed(1)}`;
}

export function formatRemaining(remaining: string) {
  return remaining && remaining !== "unknown" ? remaining : null;
}

export function formatSearchError(caught: unknown) {
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

export function isAbortError(caught: unknown) {
  return caught instanceof DOMException && caught.name === "AbortError";
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
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
