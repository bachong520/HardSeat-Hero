import { ErrorMessage } from "./features/route-search/components/ErrorMessage";
import { PageHeader } from "./features/route-search/components/PageHeader";
import { ProviderDiagnostics } from "./features/route-search/components/ProviderDiagnostics";
import { RoutePlans } from "./features/route-search/components/RoutePlans";
import { SearchForm } from "./features/route-search/components/SearchForm";
import { useRouteSearch } from "./features/route-search/use-route-search";

export function App() {
  const {
    formState,
    formActions,
    result,
    providerStatus,
    isLoading,
    error,
    handleSubmit,
    handleAbortSearch,
  } = useRouteSearch();

  return (
    <main className="min-h-screen bg-zinc-50 text-zinc-950">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 py-6 md:px-6">
        <PageHeader providerStatus={providerStatus} result={result} />
        <ProviderDiagnostics
          providerStatus={providerStatus}
          result={result}
          isLoading={isLoading}
        />
        <SearchForm
          formState={formState}
          formActions={formActions}
          isLoading={isLoading}
          onSubmit={handleSubmit}
          onAbortSearch={handleAbortSearch}
        />
        <ErrorMessage message={error} />
        <RoutePlans result={result} />
      </div>
    </main>
  );
}
