import { apiFetch, type ApiFetchInit, type TokenGetter } from "./api";

// Types mirror the M2 contract for /api/v1/budgets, /notifications/* and /alerts.
// Money arrives as decimal strings; ids are strings.

export type Severity = "info" | "warning" | "critical";
export const SEVERITIES: readonly Severity[] = ["info", "warning", "critical"];

/** Alert kinds the backend raises today; the UI also shows any other kind it sees. */
export const KNOWN_ALERT_KINDS = [
  "budget",
  "spike",
  "variance",
  "ptu_idle",
  "access_lost",
  "new_subscription",
  "unmapped_meter",
] as const;

const KIND_LABELS: Record<string, string> = {
  budget: "Budget",
  spike: "Spend spike",
  variance: "Estimate variance",
  ptu_idle: "Idle PTU",
  access_lost: "Access lost",
  new_subscription: "New subscription",
  unmapped_meter: "Unmapped meter",
};

/** "access_lost" -> "Access lost"; unknown kinds are humanised rather than hidden. */
export function alertKindLabel(kind: string): string {
  if (KIND_LABELS[kind]) return KIND_LABELS[kind];
  const words = kind.replace(/[_-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : kind;
}

// ---- Budgets ----

/** `tag` scope_value is "key:value"; `deployment` is "<account resource name>/<deployment>". */
export type BudgetScopeType = "all" | "account" | "model" | "deployment" | "tag";
export type BudgetStatus = "ok" | "warning" | "over";

export interface Budget {
  id: string;
  name: string;
  scope_type: BudgetScopeType;
  scope_value: string | null;
  amount: string;
  currency: string;
  /** Fractions of amount, e.g. [0.8, 1]. */
  thresholds: number[];
  active: boolean;
  month_to_date: string;
  forecast_month_end: string | null;
  /** Percent of amount spent so far (80 = 80%). */
  percent_used: number | string | null;
  status: BudgetStatus;
}

export interface BudgetInput {
  name: string;
  scope_type: BudgetScopeType;
  scope_value: string | null;
  amount: string;
  currency: string;
  thresholds: number[];
  active: boolean;
}

// ---- Notification channels ----

export type ChannelKind = "email" | "slack" | "teams" | "webhook";

export interface NotificationChannel {
  id: string;
  name: string;
  kind: ChannelKind;
  /** Webhook URLs carry a token, so the API only ever returns a masked form. */
  target_masked: string;
  /** Empty = all kinds. */
  alert_kinds: string[];
  min_severity: Severity;
  digest: boolean;
  active: boolean;
}

/** Created channel; generic webhooks get their signing secret here, exactly once. */
export interface CreatedChannel extends NotificationChannel {
  signing_secret?: string | null;
}

export interface ChannelInput {
  name: string;
  kind: ChannelKind;
  /** Required on create; omitted on update to keep the stored target. */
  target?: string;
  alert_kinds: string[];
  min_severity: Severity;
  digest: boolean;
  active: boolean;
}

/** Whatever the test endpoint returns; a failed send usually comes back as an HTTP error. */
export interface TestResult {
  status?: string;
  ok?: boolean;
  error?: string | null;
  detail?: string;
}

export interface Delivery {
  id: string;
  channel_id: string;
  channel_name: string | null;
  alert_event_id: string | null; // null for a digest
  status: "sent" | "failed" | string;
  error: string | null;
  attempted_at: string;
}

// ---- Alerts ----

export type AlertStatusFilter = "open" | "resolved" | "all";

export interface AlertEvent {
  id: string;
  kind: string;
  severity: Severity;
  subject: string;
  message: string;
  details: Record<string, unknown>;
  created_at: string;
  resolved_at: string | null;
  notified_at: string | null;
}

// ---- Pickers (M1 endpoints that lib/api.ts has no typed method for) ----

export interface CloudAccount {
  cloud: string;
  account_id: string;
  name: string | null;
  has_ai_resources: boolean;
  last_synced_at: string | null;
}

/** Client for budgets, notification channels / deliveries and alerts, on top of apiFetch. */
export class AlertsApi {
  constructor(private readonly getToken: TokenGetter) {}

  budgets(): Promise<Budget[]> {
    return this.call("/api/v1/budgets");
  }

  createBudget(body: BudgetInput): Promise<Budget> {
    return this.call("/api/v1/budgets", { method: "POST", json: body });
  }

  updateBudget(id: string, body: BudgetInput): Promise<Budget> {
    return this.call(`/api/v1/budgets/${encodeURIComponent(id)}`, { method: "PUT", json: body });
  }

  deleteBudget(id: string): Promise<void> {
    return this.call(`/api/v1/budgets/${encodeURIComponent(id)}`, { method: "DELETE" });
  }

  channels(): Promise<NotificationChannel[]> {
    return this.call("/api/v1/notifications/channels");
  }

  createChannel(body: ChannelInput): Promise<CreatedChannel> {
    return this.call("/api/v1/notifications/channels", { method: "POST", json: body });
  }

  updateChannel(id: string, body: ChannelInput): Promise<NotificationChannel> {
    return this.call(`/api/v1/notifications/channels/${encodeURIComponent(id)}`, {
      method: "PUT",
      json: body,
    });
  }

  deleteChannel(id: string): Promise<void> {
    return this.call(`/api/v1/notifications/channels/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  }

  testChannel(id: string): Promise<TestResult | undefined> {
    return this.call(`/api/v1/notifications/channels/${encodeURIComponent(id)}/test`, {
      method: "POST",
    });
  }

  deliveries(limit = 50): Promise<Delivery[]> {
    return this.call("/api/v1/notifications/deliveries", { query: { limit } });
  }

  alerts(status: AlertStatusFilter, kind?: string, limit = 200): Promise<AlertEvent[]> {
    return this.call("/api/v1/alerts", { query: { status, kind, limit } });
  }

  resolveAlert(id: string): Promise<AlertEvent | undefined> {
    return this.call(`/api/v1/alerts/${encodeURIComponent(id)}/resolve`, { method: "POST" });
  }

  accounts(): Promise<CloudAccount[]> {
    return this.call("/api/v1/accounts");
  }

  private call<T>(path: string, init: Omit<ApiFetchInit, "getToken"> = {}): Promise<T> {
    return apiFetch<T>(path, { ...init, getToken: this.getToken });
  }
}
