"use client";

import { useMsal } from "@azure/msal-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ConnectApi } from "@/lib/api-connect";
import { acquireArmToken } from "@/lib/armToken";
import { useSession } from "../AuthProvider";
import { errorMessage } from "./format";

/**
 * Connect client (reusing the session's API token) plus an ARM token getter. With auth disabled
 * there is no MsalProvider; useMsal then returns MSAL's stub, which acquireArmToken never calls.
 */
export function useConnectApi() {
  const { instance } = useMsal();
  const { api } = useSession();
  return useMemo(
    () => ({
      api: new ConnectApi(api.getToken),
      armToken: (interactive: boolean) => acquireArmToken(instance, { interactive }),
    }),
    [instance, api],
  );
}

/** null while loading; admins and owners may connect, track and untrack. */
export function useIsAdmin(): boolean | null {
  const { api } = useSession();
  const [isAdmin, setIsAdmin] = useState<boolean | null>(null);
  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => !cancelled && setIsAdmin(me.roles.some((r) => r === "admin" || r === "owner")))
      .catch(() => !cancelled && setIsAdmin(false));
    return () => {
      cancelled = true;
    };
  }, [api]);
  return isAdmin;
}

export type PollState<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: T; refreshError: string | null };

/**
 * Loads once, then reloads every `intervalMs` (null stops polling) while the tab is visible.
 * A failed background refresh keeps the last data and reports refreshError instead.
 */
export function usePoll<T>(load: () => Promise<T>, intervalMs: number | null) {
  const [state, setState] = useState<PollState<T>>({ status: "loading" });
  const [reloadKey, setReloadKey] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const hasData = useRef(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = () => {
      if (intervalMs !== null && !cancelled) timer = setTimeout(tick, intervalMs);
    };
    const tick = async () => {
      if (document.hidden) return schedule();
      try {
        const data = await loadRef.current();
        if (cancelled) return;
        hasData.current = true;
        setState({ status: "ready", data, refreshError: null });
      } catch (err) {
        if (cancelled) return;
        const message = errorMessage(err);
        setState((prev) =>
          prev.status === "ready" ? { ...prev, refreshError: message } : { status: "error", message },
        );
      }
      schedule();
    };

    if (!hasData.current) setState({ status: "loading" });
    void tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [intervalMs, reloadKey]);

  const reload = useCallback(() => setReloadKey((k) => k + 1), []);
  /** Replace the data locally, e.g. after a successful toggle, without waiting for the next poll. */
  const update = useCallback(
    (fn: (data: T) => T) =>
      setState((prev) => (prev.status === "ready" ? { ...prev, data: fn(prev.data) } : prev)),
    [],
  );
  return { state, reload, update };
}
