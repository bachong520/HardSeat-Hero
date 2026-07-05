import { TrainFront } from "lucide-react";
import { ProviderStatusResponse, RouteSearchResponse } from "../../../lib/api";

interface PageHeaderProps {
  providerStatus: ProviderStatusResponse | null;
  result: RouteSearchResponse | null;
}

export function PageHeader({ providerStatus, result }: PageHeaderProps) {
  return (
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
  );
}
