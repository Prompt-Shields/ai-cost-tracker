import { config } from "./config";

// Types mirror the API contract (/api/v1). Money arrives as decimal strings.

export type Role = "owner" | "admin" | "analyst" | "viewer";

export interface Me {
  oid: string;
  name: string | null;
  email: string | null;
  groups: string[];
  roles: Role[];
}

export interface DailyCostRow {
  day: string; // YYYY-MM-DD
  model: string; // "unmapped" when the meter could not be parsed
  billed_cost: string;
}

export interface DailyCosts {
  currency: string;
  rows: DailyCostRow[];
}

export interface ModelSummary {
  model: string;
  billed_cost: string;
  input_tokens: number;
  output_tokens: number;
  cost_per_1m_tokens: string | null; // null when the model has no token data
}

export interface Summary {
  currency: string;
  total_cost: string;
  by_model: ModelSummary[];
}

export interface Deployment {
  cloud: string;
  account_id: string;
  resource_id: string;
  name: string;
  model: string | null;
  model_version: string | null;
  sku: string | null;
  capacity: number | null;
  tags: Record<string, string>;
}

export interface DateRange {
  start: string; // YYYY-MM-DD, inclusive
  end: string; // YYYY-MM-DD, inclusive
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

export class ApiClient {
  constructor(private readonly getToken: TokenGetter) {}

  me(): Promise<Me> {
    return this.get("/api/v1/me");
  }

  summary(range: DateRange): Promise<Summary> {
    return this.get("/api/v1/summary", range);
  }

  dailyCosts(range: DateRange): Promise<DailyCosts> {
    return this.get("/api/v1/costs/daily", range);
  }

  deployments(): Promise<Deployment[]> {
    return this.get("/api/v1/deployments");
  }

  private async get<T>(path: string, params?: DateRange): Promise<T> {
    const url = new URL(config.apiUrl + path);
    if (params) {
      url.searchParams.set("start", params.start);
      url.searchParams.set("end", params.end);
    }
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = await this.getToken();
    if (token) headers.Authorization = `Bearer ${token}`;

    let res: Response;
    try {
      res = await fetch(url, { headers });
    } catch {
      throw new ApiError(0, `Could not reach the API at ${config.apiUrl}.`);
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = (await res.json()) as { detail?: unknown };
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        // Non-JSON error body; keep the status text.
      }
      throw new ApiError(res.status, `${res.status} ${detail}`.trim());
    }
    return (await res.json()) as T;
  }
}

/** Last `days` days ending today (UTC), inclusive. */
export function lastNDays(days: number, today: Date = new Date()): DateRange {
  const end = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const start = new Date(end);
  start.setUTCDate(end.getUTCDate() - (days - 1));
  return { start: isoDay(start), end: isoDay(end) };
}

export function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}
