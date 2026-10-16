import type { ApiClient, DateRange, Deployment } from "./api";

// Types mirror the M2 contract for /api/v1/allocation/* and /api/v1/ptu. Money is a decimal
// string; ids are strings. Calls go through ApiClient.request so the session token is added.

export type MatchType = "tag" | "name_pattern" | "account";

export interface AllocationRule {
  id: string;
  name: string;
  /** Ascending: the lowest number is tried first and the first match wins. */
  priority: number;
  match_type: MatchType;
  /** Tag key for "tag"; null otherwise. */
  match_key: string | null;
  /** Tag value, glob on "<account resource name>/<deployment>", or subscription (account) id. */
  match_value: string;
  cost_centre: string | null;
  team: string | null;
  app: string | null;
  active: boolean;
}

export type AllocationRuleInput = Omit<AllocationRule, "id">;

export interface SplitAllocation {
  cost_centre: string | null;
  team: string | null;
  app: string | null;
  percent: number;
}

export interface SplitRule {
  id?: string;
  resource_id: string;
  deployment: string;
  allocations: SplitAllocation[];
  active?: boolean;
}

export type AllocationLevel = "cost_centre" | "team" | "app";

export interface CostCentreDailyRow {
  day: string;
  /** Cost centre / team / app; "unallocated" when no rule matched. */
  key: string;
  cost: string;
  /** A split rule or shared-resource attribution contributed to this cost. */
  approximate: boolean;
}

export interface CostCentreDaily {
  currency: string;
  rows: CostCentreDailyRow[];
}

export interface UntaggedDeployment {
  id: string;
  name: string;
  account_name: string | null;
  model: string | null;
  cost: string;
  missing_tags: string[];
}

export interface UntaggedReport {
  total: string;
  currency: string;
  deployments: UntaggedDeployment[];
}

export interface PtuDeployment {
  resource_id: string;
  account_id: string;
  deployment: string;
  model: string | null;
  capacity: number | null;
  /** 0..1 */
  avg_utilization: number | string | null;
  /** 0..1 */
  peak_utilization: number | string | null;
  idle_hours: number;
  hours: number;
  ptu_cost: string;
  idle_cost: string;
  currency: string;
}

export interface PtuHourlyRow {
  hour: string; // ISO timestamp, UTC
  utilization: number | string | null; // 0..1
}

export const UNALLOCATED = "unallocated";

/** Last path segment of an Azure resource id: the Cognitive Services account name. */
export function resourceName(resourceId: string): string {
  const parts = resourceId.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? resourceId;
}

/** URL-safe base64 without padding, as the split routes expect for resource ids. */
export function b64url(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Path key for a PTU deployment: "<account resource name>/<deployment>", URL-encoded. */
export function ptuKey(d: Pick<PtuDeployment, "resource_id" | "deployment">): string {
  return encodeURIComponent(`${resourceName(d.resource_id)}/${d.deployment}`);
}

function splitPath(resourceId: string, deployment: string): string {
  return `/api/v1/allocation/splits/${b64url(resourceId)}/${encodeURIComponent(deployment)}`;
}

/** Typed calls for allocation and PTU pages; wraps the session's ApiClient. */
export class AllocApi {
  constructor(private readonly api: ApiClient) {}

  rules(): Promise<AllocationRule[]> {
    return this.api.request("/api/v1/allocation/rules");
  }

  createRule(body: AllocationRuleInput): Promise<AllocationRule> {
    return this.api.request("/api/v1/allocation/rules", { method: "POST", json: body });
  }

  updateRule(id: string, body: AllocationRuleInput): Promise<AllocationRule> {
    return this.api.request(`/api/v1/allocation/rules/${encodeURIComponent(id)}`, {
      method: "PUT",
      json: body,
    });
  }

  deleteRule(id: string): Promise<void> {
    return this.api.request(`/api/v1/allocation/rules/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  }

  splits(): Promise<SplitRule[]> {
    return this.api.request("/api/v1/allocation/splits");
  }

  putSplit(resourceId: string, deployment: string, allocations: SplitAllocation[]): Promise<SplitRule> {
    return this.api.request(splitPath(resourceId, deployment), {
      method: "PUT",
      json: { allocations },
    });
  }

  deleteSplit(resourceId: string, deployment: string): Promise<void> {
    return this.api.request(splitPath(resourceId, deployment), { method: "DELETE" });
  }

  costCentresDaily(range: DateRange, level: AllocationLevel): Promise<CostCentreDaily> {
    return this.api.request("/api/v1/allocation/cost-centres/daily", {
      query: { ...range, level },
    });
  }

  untagged(range: DateRange): Promise<UntaggedReport> {
    return this.api.request("/api/v1/allocation/untagged", { query: { ...range } });
  }

  ptu(range: DateRange): Promise<{ deployments: PtuDeployment[] }> {
    return this.api.request("/api/v1/ptu", { query: { ...range } });
  }

  ptuHourly(d: Pick<PtuDeployment, "resource_id" | "deployment">, range: DateRange): Promise<{
    rows: PtuHourlyRow[];
  }> {
    return this.api.request(`/api/v1/ptu/${ptuKey(d)}/hourly`, { query: { ...range } });
  }
}

// ---- Rule matching (client-side preview; mirrors allocation.resolve) ----

/** fnmatch-style glob: * any run, ? one char, [abc] / [!abc] classes. Case-sensitive. */
export function globToRegExp(pattern: string): RegExp {
  let re = "";
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === "*") re += ".*";
    else if (c === "?") re += ".";
    else if (c === "[") {
      const close = pattern.indexOf("]", i + 2);
      if (close === -1) {
        re += "\\[";
        continue;
      }
      let body = pattern.slice(i + 1, close);
      if (body.startsWith("!")) body = `^${body.slice(1)}`;
      re += `[${body.replace(/\\/g, "\\\\")}]`;
      i = close;
    } else re += c.replace(/[.+^${}()|\\/]/g, "\\$&");
  }
  return new RegExp(`^${re}$`, "s");
}

/** The string a name_pattern rule is matched against. */
export function matchSubject(d: Pick<Deployment, "resource_id" | "name">): string {
  return `${resourceName(d.resource_id)}/${d.name}`;
}

export function ruleMatches(rule: AllocationRuleInput, d: Deployment): boolean {
  switch (rule.match_type) {
    case "tag":
      return !!rule.match_key && d.tags[rule.match_key] === rule.match_value;
    case "name_pattern":
      return globToRegExp(rule.match_value).test(matchSubject(d));
    case "account":
      return d.account_id === rule.match_value;
    default:
      return false;
  }
}

/** Active rules in evaluation order (priority, then name for a stable tie-break). */
export function orderedRules<T extends AllocationRuleInput>(rules: T[]): T[] {
  return rules
    .filter((r) => r.active)
    .sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name));
}

/** First active rule (by priority) that matches the deployment, or null. */
export function firstMatch<T extends AllocationRuleInput>(rules: T[], d: Deployment): T | null {
  return orderedRules(rules).find((r) => ruleMatches(r, d)) ?? null;
}

export function toNumber(v: number | string | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}
