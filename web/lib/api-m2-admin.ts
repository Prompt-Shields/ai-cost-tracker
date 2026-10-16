import { apiFetch, type ApiFetchInit, type Role, type TokenGetter } from "./api";

// Types mirror the M2 contract for /api/v1/api-keys, /invoices and /audit.

/** Roles a key can act with. Keys never carry "owner". */
export type KeyRole = Exclude<Role, "owner">;

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  roles: KeyRole[];
  created_by: string | null;
  created_at: string; // ISO timestamp
  last_used_at: string | null;
  revoked_at: string | null;
}

/** Only the create response carries the full key, and only once. */
export interface CreatedApiKey extends ApiKey {
  key: string;
}

export interface InvoicePeriod {
  start: string; // YYYY-MM-DD, inclusive
  end: string; // YYYY-MM-DD, inclusive
  billed_cost: string;
  estimated_cost: string;
  currency: string;
  status: "open" | "closed";
}

export interface InvoicePeriods {
  account_id: string;
  /** null: calendar month. */
  billing_period_day: number | null;
  periods: InvoicePeriod[];
}

export interface AuditRow {
  id: string;
  at: string; // ISO timestamp
  actor_id: string;
  actor_name: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  details: unknown;
}

export interface AuditPage {
  rows: AuditRow[];
  next_before_id: string | null;
}

export interface AuditFilters {
  action?: string;
  actor?: string;
  start?: string;
  end?: string;
}

/** Client for the M2 admin endpoints, on top of lib/api.ts apiFetch. */
export class AdminApi {
  constructor(private readonly getToken: TokenGetter) {}

  apiKeys(): Promise<ApiKey[]> {
    return this.call("/api/v1/api-keys");
  }

  createApiKey(body: { name: string; roles: KeyRole[] }): Promise<CreatedApiKey> {
    return this.call("/api/v1/api-keys", { method: "POST", json: body });
  }

  revokeApiKey(id: string): Promise<ApiKey | undefined> {
    return this.call(`/api/v1/api-keys/${encodeURIComponent(id)}/revoke`, { method: "POST" });
  }

  invoicePeriods(accountId: string, count = 6): Promise<InvoicePeriods> {
    return this.call("/api/v1/invoices/periods", { query: { account_id: accountId, count } });
  }

  setBillingPeriodDay(accountId: string, day: number | null): Promise<unknown> {
    return this.call(`/api/v1/accounts/${encodeURIComponent(accountId)}/billing-period-day`, {
      method: "PUT",
      json: { billing_period_day: day },
    });
  }

  audit(filters: AuditFilters, beforeId?: string | null, limit = 100): Promise<AuditPage> {
    return this.call("/api/v1/audit", {
      query: { ...filters, limit, before_id: beforeId ?? undefined },
    });
  }

  private call<T>(path: string, init: Omit<ApiFetchInit, "getToken"> = {}): Promise<T> {
    return apiFetch<T>(path, { ...init, getToken: this.getToken });
  }
}

/** Owners hold every role; admins can change settings. */
export function isAdmin(roles: readonly Role[] | undefined): boolean {
  return !!roles && (roles.includes("owner") || roles.includes("admin"));
}
