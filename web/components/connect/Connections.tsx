"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import type { Backfill, Connection, SourceHealth, SourceName } from "@/lib/api-connect";
import { DiscoveryBanner } from "../DiscoveryBanner";
import { CopyBlock } from "./CopyBlock";
import { AccessPill, SourcePill } from "./StatusPill";
import { errorMessage, formatTimestamp, isCommand, timeAgo } from "./format";
import { useConnectApi, useIsAdmin, usePoll } from "./hooks";
import styles from "./connect.module.css";

const POLL_MS = 30_000;

const SOURCES: { key: SourceName; label: string }[] = [
  { key: "inventory", label: "Deployments" },
  { key: "cost", label: "Cost" },
  { key: "usage", label: "Token usage" },
];

/** Tracked first, then by name, so what is being pulled is at the top. */
function sortConnections(rows: Connection[]): Connection[] {
  return [...rows].sort(
    (a, b) =>
      Number(b.tracked) - Number(a.tracked) ||
      (a.name ?? a.account_id).localeCompare(b.name ?? b.account_id),
  );
}

export function Connections() {
  const { api } = useConnectApi();
  const isAdmin = useIsAdmin() === true;
  const load = useCallback(() => api.connections().then(sortConnections), [api]);
  const { state, reload, update } = usePoll(load, POLL_MS);

  return (
    <main className={styles.main}>
      <div className={styles.toolbar}>
        <div>
          <h1 className={styles.title}>Connections</h1>
          <p className={styles.lede}>
            Per subscription: when cost, token usage and deployments were last pulled, and what to
            do when something is wrong. Refreshes every 30 seconds.
          </p>
        </div>
        {isAdmin && (
          <Link href="/connect" className={styles.primary} style={{ textDecoration: "none" }}>
            Connect Azure
          </Link>
        )}
      </div>

      <DiscoveryBanner onTracked={reload} />

      {state.status === "loading" && (
        <div aria-busy="true" aria-live="polite">
          <span className={styles.srOnly}>Loading connections…</span>
          {[0, 1].map((i) => (
            <div
              key={i}
              className={`${styles.panel} ${styles.skeleton}`}
              style={{ height: 200, marginBottom: 16 }}
            />
          ))}
        </div>
      )}
      {state.status === "error" && (
        <div className={styles.notice} role="alert">
          <p className={styles.noticeTitle}>Couldn&apos;t load connections</p>
          <p className={styles.noticeBody}>{state.message}</p>
          <div className={styles.actions}>
            <button type="button" className={styles.secondary} onClick={reload}>
              Try again
            </button>
          </div>
        </div>
      )}
      {state.status === "ready" && state.data.length === 0 && (
        <div className={styles.notice}>
          <p className={styles.noticeTitle}>No subscriptions connected yet</p>
          <p className={styles.noticeBody}>
            Connect Azure to start tracking AI spend. It takes one click and the read-only Reader
            role.
          </p>
          {isAdmin && (
            <div className={styles.actions}>
              <Link href="/connect" className={styles.primary} style={{ textDecoration: "none" }}>
                Connect Azure
              </Link>
            </div>
          )}
        </div>
      )}
      {state.status === "ready" && state.data.length > 0 && (
        <>
          {state.refreshError && (
            <p className={styles.refreshNote} role="status">
              Couldn&apos;t refresh: {state.refreshError}. Showing the last loaded status.
            </p>
          )}
          {state.data.map((c) => (
            <ConnectionCard
              key={`${c.cloud}:${c.account_id}`}
              conn={c}
              isAdmin={isAdmin}
              onChange={(next) =>
                update((rows) =>
                  rows.map((r) =>
                    r.cloud === next.cloud && r.account_id === next.account_id ? next : r,
                  ),
                )
              }
            />
          ))}
        </>
      )}
    </main>
  );
}

function ConnectionCard({
  conn,
  isAdmin,
  onChange,
}: {
  conn: Connection;
  isAdmin: boolean;
  onChange: (next: Connection) => void;
}) {
  const { api } = useConnectApi();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = conn.name ?? conn.account_id;

  const setTracked = async (tracked: boolean) => {
    if (
      !tracked &&
      !window.confirm(
        `Stop tracking ${label}? Data already loaded stays; new cost and usage won't be pulled.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api.track(conn.account_id, tracked);
      // The response is the account; keep our health fields if it omits them.
      onChange({
        ...conn,
        ...res,
        tracked: res.tracked ?? tracked,
        sources: res.sources ?? conn.sources,
        backfill: res.backfill ?? conn.backfill,
      });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={styles.panel} aria-label={label}>
      <div className={styles.connHead}>
        <div style={{ minWidth: 0 }}>
          <h2 className={styles.connTitle}>{label}</h2>
          <span className={`${styles.mono} ${styles.muted}`}>
            {conn.cloud} · {conn.account_id}
          </span>
        </div>
        <div className={styles.connBadges}>
          <AccessPill status={conn.access_status} />
          {isAdmin ? (
            <label className={styles.toggle}>
              <input
                type="checkbox"
                role="switch"
                checked={conn.tracked}
                disabled={busy}
                onChange={(e) => void setTracked(e.target.checked)}
                aria-label={`Track ${label}`}
              />
              Tracked
            </label>
          ) : (
            <span className={styles.badge}>{conn.tracked ? "Tracked" : "Not tracked"}</span>
          )}
        </div>
      </div>
      {error && (
        <p className={styles.inlineError} role="alert">
          {error}
        </p>
      )}
      {conn.access_status !== "connected" && (
        <p className={styles.sourceBody}>
          {conn.access_status === "waiting_for_access"
            ? "Waiting for the subscription owner to grant the Reader role. It connects by itself once they do."
            : "The app has no Reader role on this subscription, so nothing can be pulled."}{" "}
          {isAdmin && (
            <Link href={`/connect/pending/${encodeURIComponent(conn.account_id)}`} className={styles.link}>
              Show the command
            </Link>
          )}
        </p>
      )}

      <div className={styles.sources}>
        {SOURCES.map(({ key, label: sourceLabel }) => (
          <SourceCell key={key} label={sourceLabel} health={conn.sources[key]} />
        ))}
      </div>

      {(conn.backfill.cost || conn.backfill.usage) && (
        <div className={styles.backfills}>
          {conn.backfill.cost && <BackfillBar label="Cost history (12 months)" backfill={conn.backfill.cost} />}
          {conn.backfill.usage && (
            <BackfillBar label="Token usage history (30 days)" backfill={conn.backfill.usage} />
          )}
        </div>
      )}
    </section>
  );
}

function SourceCell({ label, health }: { label: string; health: SourceHealth | undefined }) {
  const h: SourceHealth = health ?? { status: "never", last_success_at: null, message: null, fix: null };
  return (
    <div className={styles.source}>
      <div className={styles.sourceHead}>
        <span className={styles.sourceName}>{label}</span>
        <SourcePill status={h.status} />
      </div>
      <p className={styles.sourceBody}>
        Last success:{" "}
        {h.last_success_at ? (
          <time dateTime={h.last_success_at} title={formatTimestamp(h.last_success_at)}>
            {timeAgo(h.last_success_at)}
          </time>
        ) : (
          "never"
        )}
      </p>
      {h.message && <p className={styles.sourceBody}>{h.message}</p>}
      {h.fix &&
        (isCommand(h.fix) ? (
          <CopyBlock text={h.fix} label={`Fix for ${label}`} />
        ) : (
          <p className={styles.sourceBody}>
            <strong>Fix:</strong> {h.fix}
          </p>
        ))}
    </div>
  );
}

function BackfillBar({ label, backfill }: { label: string; backfill: Backfill }) {
  const pct = Math.round(Math.min(1, Math.max(0, backfill.progress)) * 100);
  const done = pct >= 100;
  return (
    <div>
      <div className={styles.barLabel}>
        <span>{label}</span>
        <span>
          {done ? "Complete" : `${pct}%`}
          {!done && backfill.done_through && ` · done through ${backfill.done_through}`}
        </span>
      </div>
      <div
        className={styles.bar}
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
      >
        <div className={`${styles.barFill} ${done ? styles.barDone : ""}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
