"use client";

import {
  InteractionRequiredAuthError,
  InteractionStatus,
  type IPublicClientApplication,
} from "@azure/msal-browser";
import { MsalProvider, useIsAuthenticated, useMsal } from "@azure/msal-react";
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiClient } from "@/lib/api";
import { config, missingAuthConfig } from "@/lib/config";
import { createMsalInstance, loginScopes } from "@/lib/msal";
import styles from "./AuthProvider.module.css";

interface Session {
  api: ApiClient;
  /** Name from the identity provider; the header falls back to /me. */
  accountName: string | null;
  /** null when auth is disabled (nothing to sign out of). */
  signOut: (() => void) | null;
}

const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession must be used inside <AuthProvider>");
  return session;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  if (config.authDisabled) return <NoAuth>{children}</NoAuth>;
  const missing = missingAuthConfig();
  if (missing.length > 0) {
    return (
      <Centered title="Sign-in is not configured">
        <p>
          Set {missing.join(", ")} or set NEXT_PUBLIC_AUTH_DISABLED=true for local development.
        </p>
      </Centered>
    );
  }
  return <EntraAuth>{children}</EntraAuth>;
}

function NoAuth({ children }: { children: ReactNode }) {
  const session = useMemo<Session>(
    () => ({ api: new ApiClient(async () => null), accountName: null, signOut: null }),
    [],
  );
  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>;
}

function EntraAuth({ children }: { children: ReactNode }) {
  const [instance, setInstance] = useState<IPublicClientApplication | null>(null);
  // Created after mount so server rendering never touches MSAL.
  useEffect(() => setInstance(createMsalInstance()), []);
  if (!instance) return <Centered title="Loading…" />;
  return (
    <MsalProvider instance={instance}>
      <SignInGate>{children}</SignInGate>
    </MsalProvider>
  );
}

function SignInGate({ children }: { children: ReactNode }) {
  const { instance, accounts, inProgress } = useMsal();
  const isAuthenticated = useIsAuthenticated();
  const account = instance.getActiveAccount() ?? accounts[0] ?? null;

  const session = useMemo<Session | null>(() => {
    if (!account) return null;
    const getToken = async () => {
      const request = { scopes: loginScopes(), account };
      try {
        return (await instance.acquireTokenSilent(request)).accessToken;
      } catch (err) {
        if (err instanceof InteractionRequiredAuthError) {
          await instance.acquireTokenRedirect(request);
          // The page navigates away; never resolve so callers don't render an error first.
          return new Promise<string>(() => {});
        }
        throw err;
      }
    };
    return {
      api: new ApiClient(getToken),
      accountName: account.name ?? account.username ?? null,
      signOut: () => void instance.logoutRedirect({ account }),
    };
  }, [instance, account]);

  if (inProgress !== InteractionStatus.None && !isAuthenticated) {
    return <Centered title="Signing in…" />;
  }
  if (!isAuthenticated || !session) {
    return (
      <Centered title="AI Cost Tracker">
        <p>Sign in with your work account to see AI spend.</p>
        <button
          type="button"
          className={styles.primary}
          onClick={() => void instance.loginRedirect({ scopes: loginScopes() })}
        >
          Sign in with Microsoft
        </button>
      </Centered>
    );
  }
  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>;
}

function Centered({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <main className={styles.centered}>
      <div className={styles.card}>
        <h1>{title}</h1>
        {children}
      </div>
    </main>
  );
}
