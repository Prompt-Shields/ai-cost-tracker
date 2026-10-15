"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { DiscoveredAccount } from "@/lib/api-connect";
import { errorMessage } from "./connect/format";
import { useConnectApi, useIsAdmin } from "./connect/hooks";
import styles from "./DiscoveryBanner.module.css";

/**
 * "N new subscriptions with AI resources found", from the daily discovery job. Renders nothing
 * when there are none or the request fails: it is a nudge, not something to report errors on.
 */
export function DiscoveryBanner({ onTracked }: { onTracked?: () => void }) {
  const { api } = useConnectApi();
  const isAdmin = useIsAdmin() === true;
  const [items, setItems] = useState<DiscoveredAccount[]>([]);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [tracked, setTracked] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api
      .discovered()
      .then((rows) => !cancelled && setItems(rows))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [api]);

  const track = async (ids: string[]) => {
    setError(null);
    setBusy((prev) => new Set([...prev, ...ids]));
    const results = await Promise.allSettled(ids.map((id) => api.track(id, true)));
    const ok = ids.filter((_, i) => results[i].status === "fulfilled");
    const failed = results.find((r): r is PromiseRejectedResult => r.status === "rejected");
    if (failed) setError(errorMessage(failed.reason));
    setItems((prev) => prev.filter((a) => !ok.includes(a.account_id)));
    setBusy((prev) => new Set([...prev].filter((id) => !ids.includes(id))));
    setTracked((n) => n + ok.length);
    if (ok.length > 0) onTracked?.();
  };

  if (items.length === 0) {
    if (tracked === 0) return null;
    return (
      <div className={styles.banner} role="status">
        <p className={styles.title}>
          Tracking {tracked} new subscription{tracked === 1 ? "" : "s"}. Cost history loads in the
          background; see <Link href="/connections">Connections</Link>.
        </p>
      </div>
    );
  }

  const n = items.length;
  return (
    <section className={styles.banner} aria-label="Newly discovered subscriptions">
      <div className={styles.head}>
        <p className={styles.title}>
          {n} new subscription{n === 1 ? "" : "s"} with AI resources found
        </p>
        {isAdmin && n > 1 && (
          <button
            type="button"
            className={styles.primary}
            disabled={busy.size > 0}
            onClick={() => void track(items.map((a) => a.account_id))}
          >
            Track all
          </button>
        )}
      </div>
      {!isAdmin && (
        <p className={styles.note}>Ask an admin to start tracking {n === 1 ? "it" : "them"}.</p>
      )}
      <ul className={styles.list}>
        {items.map((a) => (
          <li key={a.account_id} className={styles.item}>
            <span className={styles.name}>
              {a.name ?? a.account_id}
              {a.name && <span className={styles.id}>{a.account_id}</span>}
            </span>
            {isAdmin && (
              <button
                type="button"
                className={styles.secondary}
                disabled={busy.has(a.account_id)}
                onClick={() => void track([a.account_id])}
              >
                {busy.has(a.account_id) ? "Tracking…" : "Track"}
              </button>
            )}
          </li>
        ))}
      </ul>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
