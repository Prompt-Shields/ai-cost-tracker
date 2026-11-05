import { apiFetch, downloadFile, type ApiFetchInit, type TokenGetter } from "./api";
import type { AccessStatus } from "./api-connect";

// Types mirror the M4 contract for /api/v1/aws/* (W6, issue #52).

export interface AwsPermission {
  permission: string; // e.g. cloudwatch:GetMetricData
  why: string;
}

export interface AwsExport {
  account_id: string;
  bucket: string;
  prefix: string;
  name: string;
  region: string;
}

export interface AwsSetup {
  enabled: boolean;
  configured: boolean;
  missing: string[]; // ACT_* setting names still unset
  role_name: string;
  export: AwsExport | null;
  permissions: AwsPermission[];
  template_url: string | null;
  deploy_command_member: string;
  deploy_command_export: string;
  docs_path: string; // repo-relative, e.g. docs/aws-setup.md
}

export interface AwsVisibleAccount {
  account_id: string;
  name: string | null;
  has_ai_resources: boolean;
  tracked: boolean | null; // null/false when not stored yet
  access_status: AccessStatus | null;
}

/** Result of adding or re-checking an account. */
export interface AwsAccountResult {
  account_id: string;
  name: string | null;
  access_status: AccessStatus;
  quick_create_url: string | null;
  deploy_command: string | null;
}

export interface AwsAccountSetup {
  account_id: string;
  role_arn: string;
  quick_create_url: string | null;
  deploy_command: string;
}

/** Same rule as the API: an AWS account ID is exactly 12 digits. */
export const AWS_ACCOUNT_ID_RE = /^\d{12}$/;

/** Accept the common pasted forms ("1234-5678-9012", spaces) and keep only the digits. */
export function normaliseAccountId(value: string): string {
  return value.trim().replace(/[\s-]/g, "");
}

export const AWS_TEMPLATE_FILENAME = "ai-cost-tracker-readonly-role.yaml";

/** Client for the AWS onboarding endpoints, on top of lib/api.ts apiFetch. */
export class AwsApi {
  constructor(private readonly getToken: TokenGetter) {}

  setup(): Promise<AwsSetup> {
    return this.call("/api/v1/aws/setup");
  }

  /** The template needs the bearer token, so a plain link won't do; fetch and save it. */
  downloadTemplate(): Promise<void> {
    return downloadFile("/api/v1/aws/template.yaml", {
      getToken: this.getToken,
      filename: AWS_TEMPLATE_FILENAME,
    });
  }

  visibleAccounts(): Promise<AwsVisibleAccount[]> {
    return this.call("/api/v1/aws/accounts/visible");
  }

  addAccount(accountId: string): Promise<AwsAccountResult> {
    return this.call("/api/v1/aws/accounts", { method: "POST", json: { account_id: accountId } });
  }

  accountSetup(accountId: string): Promise<AwsAccountSetup> {
    return this.call(`/api/v1/aws/accounts/${encodeURIComponent(accountId)}/setup`);
  }

  check(accountId: string): Promise<AwsAccountResult> {
    return this.call(`/api/v1/aws/accounts/${encodeURIComponent(accountId)}/check`, {
      method: "POST",
    });
  }

  private call<T>(path: string, init: Omit<ApiFetchInit, "getToken"> = {}): Promise<T> {
    return apiFetch<T>(path, { ...init, getToken: this.getToken });
  }
}
