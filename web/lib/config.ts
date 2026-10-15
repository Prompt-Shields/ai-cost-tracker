// NEXT_PUBLIC_* values are inlined at build time, so they must be referenced literally.
export const config = {
  apiUrl: (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/+$/, ""),
  entraClientId: process.env.NEXT_PUBLIC_ENTRA_CLIENT_ID || "",
  entraTenantId: process.env.NEXT_PUBLIC_ENTRA_TENANT_ID || "",
  apiScope: process.env.NEXT_PUBLIC_API_SCOPE || "",
  authDisabled: process.env.NEXT_PUBLIC_AUTH_DISABLED === "true",
} as const;

/** Names of required auth settings that are missing; empty when auth is disabled. */
export function missingAuthConfig(): string[] {
  if (config.authDisabled) return [];
  const missing: string[] = [];
  if (!config.entraClientId) missing.push("NEXT_PUBLIC_ENTRA_CLIENT_ID");
  if (!config.entraTenantId) missing.push("NEXT_PUBLIC_ENTRA_TENANT_ID");
  if (!config.apiScope) missing.push("NEXT_PUBLIC_API_SCOPE");
  return missing;
}
