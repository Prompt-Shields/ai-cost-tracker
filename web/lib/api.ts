import { config } from "./config";

// Types mirror the API contract (/api/v1). Money arrives as decimal strings.

/** Base URL of the API, without a trailing slash. Paths passed to apiFetch start with /api/v1. */
export const API_BASE_URL = config.apiUrl;

export type Role = "owner" | "admin" | "analyst" | "viewer";

export interface Me {
  oid: string;
  name: string | null;
  email: string | null;
  groups: string[];
  roles: Role[];
}

/** estimated: no billed cost yet (show estimated_cost); billed: may still be restated; finalised: closed. */
export type DayStatus = "estimated" | "billed" | "finalised";

/** `account` is a subscription on Azure; `tag:<key>` groups by that tag's value. */
export type GroupBy = "model" | "deployment" | "account" | `tag:${string}`;

export interface DailyGroupRow {
  day: string; // YYYY-MM-DD
  key: string; // model / deployment / account id / tag value, per group_by
  billed_cost: string;
  estimated_cost: string;
  day_status: DayStatus;
}

export interface DailyCosts {
  currency: string;
  rows: DailyGroupRow[];
}

export interface ModelSummary {
  model: string;
  billed_cost: string;
  estimated_cost: string;
  input_tokens: number;
  output_tokens: number;
  cost_per_1m_tokens: string | null; // null when the model has no token data
  cost_per_1m_input: string | null;
  cost_per_1m_output: string | null;
}

export interface Summary {
  currency: string;
  total_cost: string;
  today_estimated_cost: string;
  month_to_date_cost: string;
  by_model: ModelSummary[];
}

export interface Deployment {
  id: string;
  cloud: string;
  account_id: string;
  account_name: string | null;
  resource_id: string;
  name: string;
  model: string | null;
  model_version: string | null;
  sku: string | null;
  capacity: number | null;
  region: string | null;
  tags: Record<string, string>;
}

export interface DeploymentDailyRow {
  day: string;
  billed_cost: string;
  estimated_cost: string;
  input_tokens: number;
  output_tokens: number;
  cost_per_1m_tokens: string | null;
  day_status: DayStatus;
}

export interface DeploymentDaily {
  deployment: Deployment;
  rows: DeploymentDailyRow[];
}

export interface DateRange {
  start: string; // YYYY-MM-DD, inclusive
  end: string; // YYYY-MM-DD, inclusive
}

/** Optional filters shared by /summary and /costs/daily. `tag` is "key:value". */
export interface CostFilters {
  account_id?: string;
  model?: string;
  tag?: string;
}

/** Returns a bearer token, or null to call the API unauthenticated (auth disabled). */
export type TokenGetter = () => Promise<string | null>;

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export type Query = Record<string, string | number | null | undefined>;

export interface ApiFetchInit extends Omit<RequestInit, "body"> {
  /** Supplies the bearer token; omit (or return null) to call without one. */
  getToken?: TokenGetter;
  /** Query parameters; null/undefined/"" values are dropped. */
  query?: Query;
  /** Sent as a JSON body with Content-Type: application/json. */
  json?: unknown;
  body?: BodyInit | null;
}

/**
 * The one place that talks HTTP to the API: base URL, bearer token, JSON, error mapping.
 * Stable for other modules (lib/api-connect.ts). Resolves to undefined for 204 / empty bodies.
 */
export async function apiFetch<T>(path: string, init: ApiFetchInit = {}): Promise<T> {
  const { getToken, query, json, headers: extraHeaders, ...rest } = init;
  const url = new URL(API_BASE_URL + path);
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  const headers = new Headers(extraHeaders);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  let body = rest.body;
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }
  const token = getToken ? await getToken() : null;
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let res: Response;
  try {
    res = await fetch(url, { ...rest, headers, body });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, `Could not reach the API at ${API_BASE_URL}.`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = (await res.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
    } catch {
      // Non-JSON error body; keep the status text.
    }
    throw new ApiError(res.status, `${res.status} ${detail}`.trim());
  }
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/** Typed calls for the dashboard; one per signed-in session (see AuthProvider). */
export class ApiClient {
  constructor(public readonly getToken: TokenGetter) {}

  /** Escape hatch for endpoints without a typed method; adds this session's token. */
  request<T>(path: string, init: Omit<ApiFetchInit, "getToken"> = {}): Promise<T> {
    return apiFetch<T>(path, { ...init, getToken: this.getToken });
  }

  me(): Promise<Me> {
    return this.request("/api/v1/me");
  }

  summary(range: DateRange, filters: CostFilters = {}): Promise<Summary> {
    return this.request("/api/v1/summary", { query: { ...range, ...filters } });
  }

  dailyCosts(range: DateRange, groupBy: GroupBy, filters: CostFilters = {}): Promise<DailyCosts> {
    return this.request("/api/v1/costs/daily", {
      query: { ...range, group_by: groupBy, ...filters },
    });
  }

  deployments(): Promise<Deployment[]> {
    return this.request("/api/v1/deployments");
  }

  deploymentDaily(id: string, range: DateRange): Promise<DeploymentDaily> {
    return this.request(`/api/v1/deployments/${encodeURIComponent(id)}/daily`, {
      query: { ...range },
    });
  }
}

/** Last `days` days ending today (UTC), inclusive. */
export function lastNDays(days: number, today: Date = new Date()): DateRange {
  const end = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const start = new Date(end);
  start.setUTCDate(end.getUTCDate() - (days - 1));
  return { start: isoDay(start), end: isoDay(end) };
}

/** Yesterday (UTC) as a one-day range. Billing days are UTC calendar days. */
export function yesterday(today: Date = new Date()): DateRange {
  const d = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() - 1));
  return { start: isoDay(d), end: isoDay(d) };
}

export function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** What a day's cost is: billed once billing has it, the estimate until then. */
export function shownCost(row: { billed_cost: string; estimated_cost: string; day_status: DayStatus }): number {
  return Number(row.day_status === "estimated" ? row.estimated_cost : row.billed_cost);
}

/** Filename from a Content-Disposition header, falling back to `fallback`. */
function attachmentName(header: string | null, fallback: string): string {
  const m = header?.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  return m ? decodeURIComponent(m[1]) : fallback;
}

/**
 * Downloads a file endpoint (e.g. /api/v1/exports/summary.csv) and saves it. A plain link
 * can't carry the bearer token, so this fetches the body as a blob and clicks an object URL.
 */
export async function downloadFile(
  path: string,
  { getToken, query, filename }: { getToken?: TokenGetter; query?: Query; filename?: string } = {},
): Promise<void> {
  const url = new URL(API_BASE_URL + path);
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  const headers = new Headers({ Accept: "text/csv, */*" });
  const token = getToken ? await getToken() : null;
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let res: Response;
  try {
    res = await fetch(url, { headers });
  } catch {
    throw new ApiError(0, `Could not reach the API at ${API_BASE_URL}.`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = (await res.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
    } catch {
      // Non-JSON error body; keep the status text.
    }
    throw new ApiError(res.status, `${res.status} ${detail}`.trim());
  }
  const blob = await res.blob();
  const name = attachmentName(
    res.headers.get("Content-Disposition"),
    filename ?? path.split("/").pop() ?? "export.csv",
  );
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Give the browser a moment to start the download before releasing the blob.
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}
