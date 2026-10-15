import {
  EventType,
  PublicClientApplication,
  type AuthenticationResult,
  type EventMessage,
} from "@azure/msal-browser";
import { config } from "./config";

export const loginScopes = (): string[] => [config.apiScope];

/** Browser-only: MSAL touches window/sessionStorage, so never call this during SSR. */
export function createMsalInstance(): PublicClientApplication {
  const instance = new PublicClientApplication({
    auth: {
      clientId: config.entraClientId,
      authority: `https://login.microsoftonline.com/${config.entraTenantId}`,
      redirectUri: window.location.origin,
      postLogoutRedirectUri: window.location.origin,
    },
    cache: { cacheLocation: "sessionStorage" },
  });

  // The API token is per-account; keep the signed-in account active after a redirect.
  instance.addEventCallback((event: EventMessage) => {
    if (event.eventType === EventType.LOGIN_SUCCESS && event.payload) {
      instance.setActiveAccount((event.payload as AuthenticationResult).account);
    }
  });
  return instance;
}
