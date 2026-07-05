import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  ProviderStatusResponse,
  RouteSearchRequest,
  RouteSearchResponse,
  getProviderStatus,
  searchRoutesStream,
} from "../../lib/api";
import { formatSearchError, isAbortError, parseOptionalNumber } from "./formatters";
import { CandidateStrategy, RouteSearchFormActions, RouteSearchFormState } from "./types";

const today = new Date().toISOString().slice(0, 10);

const initialFormState: RouteSearchFormState = {
  fromStation: "北京",
  toStation: "上海",
  date: today,
  minTransferMinutes: 30,
  maxTransfers: 1,
  candidateLimit: 50,
  candidateStrategy: "balanced",
  maxDetourRatio: "",
  maxDetourKm: "",
  corridorWidthKm: "",
  maxTotalDurationMinutes: "",
};

export function useRouteSearch() {
  const [formState, setFormState] = useState<RouteSearchFormState>(initialFormState);
  const [result, setResult] = useState<RouteSearchResponse | null>(null);
  const [providerStatus, setProviderStatus] =
    useState<ProviderStatusResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let ignore = false;

    async function loadProviderStatus() {
      try {
        const status = await getProviderStatus();
        if (!ignore) {
          setProviderStatus(status);
        }
      } catch {
        if (!ignore) {
          setProviderStatus(null);
        }
      }
    }

    loadProviderStatus();
    return () => {
      ignore = true;
    };
  }, []);

  const formActions = useMemo<RouteSearchFormActions>(
    () => ({
      setFromStation: (value) => setFormState((state) => ({ ...state, fromStation: value })),
      setToStation: (value) => setFormState((state) => ({ ...state, toStation: value })),
      setDate: (value) => setFormState((state) => ({ ...state, date: value })),
      setMinTransferMinutes: (value) =>
        setFormState((state) => ({ ...state, minTransferMinutes: value })),
      setMaxTransfers: (value) =>
        setFormState((state) => ({ ...state, maxTransfers: value })),
      setCandidateLimit: (value) =>
        setFormState((state) => ({ ...state, candidateLimit: value })),
      setCandidateStrategy: (value: CandidateStrategy) =>
        setFormState((state) => ({ ...state, candidateStrategy: value })),
      setMaxDetourRatio: (value) =>
        setFormState((state) => ({ ...state, maxDetourRatio: value })),
      setMaxDetourKm: (value) =>
        setFormState((state) => ({ ...state, maxDetourKm: value })),
      setCorridorWidthKm: (value) =>
        setFormState((state) => ({ ...state, corridorWidthKm: value })),
      setMaxTotalDurationMinutes: (value) =>
        setFormState((state) => ({ ...state, maxTotalDurationMinutes: value })),
    }),
    [],
  );

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    abortControllerRef.current?.abort();
    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    setIsLoading(true);
    setError(null);
    setResult(null);

    try {
      const response = await searchRoutesStream(
        buildRouteSearchPayload(formState),
        (partialResponse) => setResult(partialResponse),
        abortController.signal,
      );
      setResult(response);
      setProviderStatus(await getProviderStatus());
    } catch (caught) {
      if (!isAbortError(caught)) {
        setError(formatSearchError(caught));
      }
    } finally {
      if (abortControllerRef.current === abortController) {
        abortControllerRef.current = null;
      }
      setIsLoading(false);
    }
  }

  function handleAbortSearch() {
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
    setIsLoading(false);
  }

  return {
    formState,
    formActions,
    result,
    providerStatus,
    isLoading,
    error,
    handleSubmit,
    handleAbortSearch,
  };
}

function buildRouteSearchPayload(formState: RouteSearchFormState): RouteSearchRequest {
  return {
    from_station: formState.fromStation,
    to_station: formState.toStation,
    date: formState.date,
    max_transfers: formState.maxTransfers,
    min_transfer_minutes: formState.minTransferMinutes,
    candidate_limit: formState.candidateLimit,
    candidate_strategy: formState.candidateStrategy,
    max_detour_ratio: parseOptionalNumber(formState.maxDetourRatio),
    max_detour_km: parseOptionalNumber(formState.maxDetourKm),
    corridor_width_km: parseOptionalNumber(formState.corridorWidthKm),
    max_total_duration_minutes: parseOptionalNumber(
      formState.maxTotalDurationMinutes,
    ),
  };
}
