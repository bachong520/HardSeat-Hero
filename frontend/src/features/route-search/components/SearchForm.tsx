import { FormEvent } from "react";
import { Loader2, Search, Square } from "lucide-react";
import { Button } from "../../../components/ui/button";
import { Input } from "../../../components/ui/input";
import {
  CandidateStrategy,
  RouteSearchFormActions,
  RouteSearchFormState,
} from "../types";
import { Field } from "./Field";

interface SearchFormProps {
  formState: RouteSearchFormState;
  formActions: RouteSearchFormActions;
  isLoading: boolean;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onAbortSearch: () => void;
}

const selectClassName =
  "h-10 w-full rounded-md border border-zinc-300 bg-white px-3 text-sm outline-none transition focus:border-zinc-950";

export function SearchForm({
  formState,
  formActions,
  isLoading,
  onSubmit,
  onAbortSearch,
}: SearchFormProps) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 shadow-sm">
      <form
        className="grid gap-4 md:grid-cols-[1fr_1fr_180px_170px_auto] md:items-end"
        onSubmit={onSubmit}
      >
        <Field label="出发地">
          <Input
            value={formState.fromStation}
            onChange={(event) => formActions.setFromStation(event.target.value)}
          />
        </Field>
        <Field label="目的地">
          <Input
            value={formState.toStation}
            onChange={(event) => formActions.setToStation(event.target.value)}
          />
        </Field>
        <Field label="日期">
          <Input
            type="date"
            value={formState.date}
            onChange={(event) => formActions.setDate(event.target.value)}
          />
        </Field>
        <Field label="最短换乘">
          <Input
            type="number"
            min={0}
            max={360}
            value={formState.minTransferMinutes}
            onChange={(event) =>
              formActions.setMinTransferMinutes(Number(event.target.value))
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
          <Button type="button" onClick={onAbortSearch}>
            <Square className="h-4 w-4" />
            中断
          </Button>
        ) : null}
        <div className="grid gap-3 rounded-md border border-dashed border-zinc-200 bg-zinc-50 p-3 md:col-span-full md:grid-cols-4">
          <Field label="最大中转次数">
            <select
              className={selectClassName}
              value={formState.maxTransfers}
              onChange={(event) =>
                formActions.setMaxTransfers(Number(event.target.value))
              }
            >
              <option value={0}>0 次（只查直达）</option>
              <option value={1}>1 次</option>
              <option value={2}>2 次</option>
            </select>
          </Field>
          <Field label="搜索策略">
            <select
              className={selectClassName}
              value={formState.candidateStrategy}
              onChange={(event) =>
                formActions.setCandidateStrategy(
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
              value={formState.candidateLimit}
              onChange={(event) =>
                formActions.setCandidateLimit(Number(event.target.value))
              }
            />
          </Field>
          <Field label="最大总耗时（分钟，可空）">
            <Input
              type="number"
              min={60}
              value={formState.maxTotalDurationMinutes}
              onChange={(event) =>
                formActions.setMaxTotalDurationMinutes(event.target.value)
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
              value={formState.maxDetourRatio}
              onChange={(event) =>
                formActions.setMaxDetourRatio(event.target.value)
              }
              placeholder="策略默认"
            />
          </Field>
          <Field label="最大绕行公里，可空">
            <Input
              type="number"
              min={0}
              max={5000}
              value={formState.maxDetourKm}
              onChange={(event) => formActions.setMaxDetourKm(event.target.value)}
              placeholder="策略默认"
            />
          </Field>
          <Field label="走廊宽度 km，可空">
            <Input
              type="number"
              min={1}
              max={2000}
              value={formState.corridorWidthKm}
              onChange={(event) =>
                formActions.setCorridorWidthKm(event.target.value)
              }
              placeholder="策略默认"
            />
          </Field>
          <div className="text-xs leading-5 text-zinc-500 md:col-span-1 md:self-end">
            搜索越深，请求越多、耗时越长，但更不容易漏掉绕行中转方案。
          </div>
        </div>
      </form>
    </section>
  );
}
