import { RouteSearchRequest } from "../../lib/api";

export type CandidateStrategy = NonNullable<
  RouteSearchRequest["candidate_strategy"]
>;

export interface RouteSearchFormState {
  fromStation: string;
  toStation: string;
  date: string;
  minTransferMinutes: number;
  maxTransfers: number;
  candidateLimit: number;
  candidateStrategy: CandidateStrategy;
  maxDetourRatio: string;
  maxDetourKm: string;
  corridorWidthKm: string;
  maxTotalDurationMinutes: string;
}

export type RouteSearchFormField = keyof RouteSearchFormState;

export type RouteSearchFormActions = {
  [Field in RouteSearchFormField as `set${Capitalize<Field>}`]: (
    value: RouteSearchFormState[Field],
  ) => void;
};
