import {
  BrowserAuthError,
  BrowserAuthErrorCodes,
  InteractionRequiredAuthError,
  type AccountInfo,
  type IPublicClientApplication,
} from "@azure/msal-browser";
import { config } from "./config";

/** Delegated Azure Resource Manager scope: the API acts with the signed-in user's own rights. */
export const ARM_SCOPE = "https://management.azure.com/user_impersonation";

/** Thrown by a non-interactive request when Entra needs the user to consent or sign in again. */
export class ArmConsentRequired extends Error {
  constructor() {
    super("Azure needs you to confirm access to your subscriptions.");
    this.name = "ArmConsentRequired";
  }
}

function activeAccount(instance: IPublicClientApplication): AccountInfo | null {
  return instance.getActiveAccount() ?? instance.getAllAccounts()[0] ?? null;
}

/** Never settles: used after starting a redirect so callers don't flash an error first. */
const navigatingAway = <T>(): Promise<T> => new Promise<T>(() => {});

/**
 * ARM token for the connect flow, or null when auth is disabled (local dev has no Entra
 * session, so the connect page explains that instead of calling the API).
 *
 * Silent first. With `interactive` (call it from a click, or browsers block the popup) it falls
 * back to a popup, then to a redirect that returns to this page. Without it, it throws
 * ArmConsentRequired so the page can ask for a click. The token is only passed to the API for
 * the request that needs it; it is never stored by this app (MSAL keeps its own session cache).
 */
export async function acquireArmToken(
  instance: IPublicClientApplication,
  { interactive }: { interactive: boolean },
): Promise<string | null> {
  if (config.authDisabled) return null;
  const account = activeAccount(instance);
  const request = { scopes: [ARM_SCOPE], account: account ?? undefined };
  if (account) {
    try {
      return (await instance.acquireTokenSilent({ ...request, account })).accessToken;
    } catch (err) {
      if (!(err instanceof InteractionRequiredAuthError)) throw err;
    }
  }
  if (!interactive) throw new ArmConsentRequired();
  try {
    return (await instance.acquireTokenPopup(request)).accessToken;
  } catch (err) {
    const blocked =
      err instanceof BrowserAuthError &&
      (err.errorCode === BrowserAuthErrorCodes.popupWindowError ||
        err.errorCode === BrowserAuthErrorCodes.emptyWindowError ||
        err.errorCode === BrowserAuthErrorCodes.blockNestedPopups);
    if (!blocked) throw err;
    await instance.acquireTokenRedirect({ ...request, redirectStartPage: window.location.href });
    return navigatingAway();
  }
}
