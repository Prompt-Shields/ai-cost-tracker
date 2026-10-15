import { apiFetch, type ApiFetchInit, type TokenGetter } from "./api";

// Types mirror the M1 contract for /api/v1/azure/* and /api/v1/connections*.

export type AccessStatus = "connected" | "waiting_for_access" | "no_access";

export interface DiscoveredSubscription {
  subscription_id: string;
  name: string;
  ai_accounts: number;
  ai_deployments: number;
  tracked: boolean | null; // null when the subscription is not in our DB yet
  access_status: AccessStatus | null;
}

export interface ConnectRequest {
  subscription_ids: string[];
  management_group_id: string | null;
}

export interface ConnectResult {
  subscription_id: string;
  status: "connected" | "waiting_for_access";
  command: string | null;
  share_url: string | null;
}

export interface PendingAccess {
  subscription_id: string;
  name: string | null;
  status: AccessStatus;
  command: string | null;
  share_url: string | null;
}

export type SourceName = "inventory" | "cost" | "usage";
export type SourceStatus = "ok" | "stale" | "error" | "never";

export interface SourceHealth {
  status: SourceStatus;
  last_success_at: string | null; // ISO timestamp
  message: string | null;
  fix: string | null; // plain-language fix, or a command to run
}

export interface Backfill {
  progress: number; // 0..1
  from: string | null; // YYYY-MM-DD
  done_through: string | null;
}

export interface Connection {
  cloud: string;
  account_id: string;
  name: string | null;
  tracked: boolean;
  access_status: AccessStatus;
  sources: Partial<Record<SourceName, SourceHealth>>;
  backfill: { cost: Backfill | null; usage: Backfill | null };
}

export interface DiscoveredAccount {
  account_id: string;
  name: string | null;
  discovered_at: string;
}

/** The delegated ARM token travels only in this header, only on discover/connect. */
const ARM_HEADER = "X-Azure-Management-Token";

/** Client for the connect / connection-health endpoints, on top of lib/api.ts apiFetch. */
export class ConnectApi {
  constructor(private readonly getToken: TokenGetter) {}

  discover(armToken: string): Promise<{ subscriptions: DiscoveredSubscription[] }> {
    return this.call("/api/v1/azure/discover", { method: "POST", armToken });
  }

  connect(armToken: string, body: ConnectRequest): Promise<{ results: ConnectResult[] }> {
    return this.call("/api/v1/azure/connect", { method: "POST", armToken, json: body });
  }

  pending(subscriptionId: string): Promise<PendingAccess> {
    return this.call(`/api/v1/azure/pending/${encodeURIComponent(subscriptionId)}`);
  }

  connections(): Promise<Connection[]> {
    return this.call("/api/v1/connections");
  }

  discovered(): Promise<DiscoveredAccount[]> {
    return this.call("/api/v1/connections/discovered");
  }

  track(accountId: string, tracked: boolean): Promise<Connection> {
    return this.call(`/api/v1/connections/${encodeURIComponent(accountId)}/track`, {
      method: "POST",
      json: { tracked },
    });
  }

  private call<T>(
    path: string,
    { armToken, ...init }: Omit<ApiFetchInit, "getToken"> & { armToken?: string } = {},
  ): Promise<T> {
    const headers = new Headers(init.headers);
    if (armToken) headers.set(ARM_HEADER, armToken);
    return apiFetch<T>(path, { ...init, headers, getToken: this.getToken });
  }
}
