import { apiFetch, type ApiFetchInit, type TokenGetter } from "./api";
import type { AccessStatus } from "./api-connect";

// Types mirror the M3 contract for /api/v1/gcp/* (W8, issue #46).

export interface GcpRole {
  role: string; // e.g. roles/aiplatform.viewer
  scope: string; // where it is granted: project, billing export dataset, ...
  why: string;
}

export interface GcpSetup {
  enabled: boolean;
  configured: boolean;
  missing: string[]; // ACT_* setting names still unset
  service_account: string | null;
  billing_export_table: string | null;
  roles: GcpRole[];
  setup_script: string;
  billing_grant_commands: string | null;
  docs_path: string; // repo-relative, e.g. docs/gcp-setup.md
}

export interface GcpVisibleProject {
  project_id: string;
  name: string | null;
  has_ai_resources: boolean;
  tracked: boolean | null; // null/false when not stored yet
  access_status: AccessStatus | null;
}

/** Result of adding or re-checking a project. */
export interface GcpProjectResult {
  project_id: string;
  name: string | null;
  access_status: AccessStatus;
  grant_commands: string | null;
}

export interface GcpGrant {
  project_id: string;
  service_account: string | null;
  commands: string;
}

/** Same rule as the API: 6–30 chars, lowercase letters, digits and hyphens, starts with a letter. */
export const GCP_PROJECT_ID_RE = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;

/** Client for the GCP onboarding endpoints, on top of lib/api.ts apiFetch. */
export class GcpApi {
  constructor(private readonly getToken: TokenGetter) {}

  setup(): Promise<GcpSetup> {
    return this.call("/api/v1/gcp/setup");
  }

  visibleProjects(): Promise<GcpVisibleProject[]> {
    return this.call("/api/v1/gcp/projects/visible");
  }

  addProject(projectId: string): Promise<GcpProjectResult> {
    return this.call("/api/v1/gcp/projects", { method: "POST", json: { project_id: projectId } });
  }

  grant(projectId: string): Promise<GcpGrant> {
    return this.call(`/api/v1/gcp/projects/${encodeURIComponent(projectId)}/grant`);
  }

  check(projectId: string): Promise<GcpProjectResult> {
    return this.call(`/api/v1/gcp/projects/${encodeURIComponent(projectId)}/check`, {
      method: "POST",
    });
  }

  private call<T>(path: string, init: Omit<ApiFetchInit, "getToken"> = {}): Promise<T> {
    return apiFetch<T>(path, { ...init, getToken: this.getToken });
  }
}
