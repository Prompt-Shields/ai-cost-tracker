"use client";

import { useCallback, useId, useMemo, useRef, useState } from "react";
import {
  KNOWN_ALERT_KINDS,
  alertKindLabel,
  type AlertEvent,
  type AlertStatusFilter,
} from "@/lib/api-m2-alerts";
import { errorMessage } from "../connect/format";
import { useIsAdmin, usePoll } from "../connect/hooks";
import connect from "../connect/connect.module.css";
import { ui } from "../ui";
import { SeverityPill, When, m2, useAlertsApi } from "./shared";

const TABS: { value: AlertStatusFilter; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "resolved", label: "Resolved" },
  { value: "all", label: "All" },
];

const SEVERITY_RANK = { critical: 0, warning: 1, info: 2 } as const;

/** Open first, then most severe, then newest: the top of the list is what to look at. */
function sortAlerts(rows: AlertEvent[]): AlertEvent[] {
  return [...rows].sort(
    (a, b) =>
      Number(a.resolved_at !== null) - Number(b.resolved_at !== null) ||
      (SEVERITY_RANK[a.severity] ?? 3) - (SEVERITY_RANK[b.severity] ?? 3) ||
      Date.parse(b.created_at) - Date.parse(a.created_at),
  );
}

export function AlertsInbox() {
  const isAdmin = useIsAdmin() === true;
  const [tab, setTab] = useState<AlertStatusFilter>("open");
  const [kind, setKind] = useState("");
  const [seenKinds, setSeenKinds] = useState<string[]>([]);
  const kindOptions = useMemo(
    () => [...new Set([...KNOWN_ALERT_KINDS, ...seenKinds, ...(kind ? [kind] : [])])],
    [seenKinds, kind],
  );

  return (
    <main className={ui.main}>
      <div className={ui.toolbar}>
        <div>
          <h1 className={ui.title}>Alerts</h1>
          <p className={m2.lede}>
            Budget thresholds, spend spikes, idle PTU, estimate drift and connection problems.
            Alerts resolve by themselves when the condition clears. Refreshes every minute.
          </p>
        </div>
      </div>

      <div className={m2.filterBar}>
        <div className={m2.tabs} role="tablist" aria-label="Alert status">
          {TABS.map((t) => (
            <button
              key={t.value}
              type="button"
              role="tab"
              aria-selected={tab === t.value}
              className={m2.tab}
              onClick={() => setTab(t.value)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <label className={m2.check} style={{ cursor: "default" }}>
          <span className={m2.dim} style={{ fontSize: 14 }}>
            Type
          </span>
          <select className={m2.select} value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">All types</option>
            {kindOptions.map((k) => (
              <option key={k} value={k}>
                {alertKindLabel(k)}
              </option>
            ))}
          </select>
        </label>
      </div>

      <AlertsList
        key={`${tab}|${kind}`}
        tab={tab}
        kind={kind}
        isAdmin={isAdmin}
        onKinds={(kinds) => setSeenKinds((prev) => [...new Set([...prev, ...kinds])])}
      />
    </main>
  );
}

/** Keyed by the filters, so switching tabs shows a fresh loading state, never the old list. */
function AlertsList({
  tab,
  kind,
  isAdmin,
  onKinds,
}: {
  tab: AlertStatusFilter;
  kind: string;
  isAdmin: boolean;
  onKinds: (kinds: string[]) => void;
}) {
  const api = useAlertsApi();
  const onKindsRef = useRef(onKinds);
  onKindsRef.current = onKinds;
  const load = useCallback(
    () =>
      api.alerts(tab, kind || undefined).then((rows) => {
        onKindsRef.current(rows.map((r) => r.kind));
        return sortAlerts(rows);
      }),
    [api, tab, kind],
  );
  const { state, reload, update } = usePoll(load, 60_000);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const resolve = async (a: AlertEvent) => {
    setBusy(a.id);
    setRowErrors(({ [a.id]: _, ...rest }) => rest);
    try {
      const res = await api.resolveAlert(a.id);
      const resolvedAt = res?.resolved_at ?? new Date().toISOString();
      update((rows) =>
        tab === "open"
          ? rows.filter((r) => r.id !== a.id)
          : rows.map((r) => (r.id === a.id ? { ...r, ...(res ?? {}), resolved_at: resolvedAt } : r)),
      );
    } catch (err) {
      setRowErrors((e) => ({ ...e, [a.id]: `Couldn't resolve: ${errorMessage(err)}` }));
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
        {state.status === "loading" && (
          <div className={`${ui.panel} ${ui.skeleton}`} style={{ height: 260 }} aria-busy="true">
            <span className={ui.srOnly}>Loading alerts…</span>
          </div>
        )}
        {state.status === "error" && (
          <div className={ui.notice} role="alert">
            <p className={ui.noticeTitle}>Couldn&apos;t load alerts</p>
            <p className={ui.noticeBody}>{state.message}</p>
            <button type="button" className={ui.retry} onClick={reload}>
              Try again
            </button>
          </div>
        )}
        {state.status === "ready" && state.data.length === 0 && (
          <div className={ui.notice}>
            <p className={ui.noticeTitle}>
              {tab === "open" ? "No open alerts" : tab === "resolved" ? "No resolved alerts" : "No alerts"}
              {kind && ` of type “${alertKindLabel(kind)}”`}
            </p>
            <p className={ui.noticeBody}>
              {tab === "open"
                ? "Nothing needs attention right now."
                : "Alerts appear here once a budget, spike or connection check raises one."}
            </p>
          </div>
        )}
        {state.status === "ready" && state.data.length > 0 && (
          <section className={ui.panel} aria-label="Alerts">
            {state.refreshError && (
              <p className={connect.refreshNote} role="status" style={{ marginTop: 0 }}>
                Couldn&apos;t refresh: {state.refreshError}. Showing the last loaded alerts.
              </p>
            )}
            <p className={m2.dim} style={{ fontSize: 13, margin: "0 0 8px" }}>
              {state.data.length} alert{state.data.length === 1 ? "" : "s"}
            </p>
            <ul className={m2.list}>
              {state.data.map((a) => (
                <AlertItem
                  key={a.id}
                  alert={a}
                  isAdmin={isAdmin}
                  busy={busy === a.id}
                  error={rowErrors[a.id]}
                  onResolve={() => void resolve(a)}
                />
              ))}
            </ul>
          </section>
        )}
    </>
  );
}

function AlertItem({
  alert: a,
  isAdmin,
  busy,
  error,
  onResolve,
}: {
  alert: AlertEvent;
  isAdmin: boolean;
  busy: boolean;
  error: string | undefined;
  onResolve: () => void;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const resolved = a.resolved_at !== null;
  return (
    <li className={m2.item} style={resolved ? { opacity: 0.8 } : undefined}>
      <div className={m2.itemHead}>
        <div className={m2.meta} style={{ marginTop: 0 }}>
          <SeverityPill severity={a.severity} />
          <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>{alertKindLabel(a.kind)}</span>
          {resolved && <span className={connect.badge}>Resolved</span>}
        </div>
        {isAdmin && !resolved && (
          <button type="button" className={connect.secondary} onClick={onResolve} disabled={busy}>
            {busy ? "Resolving…" : "Resolve"}
          </button>
        )}
      </div>
      <p className={m2.message}>{a.message}</p>
      <div className={m2.meta}>
        <span>
          Raised <When iso={a.created_at} />
        </span>
        <span>
          Notified <When iso={a.notified_at} empty="not yet" />
        </span>
        {resolved && (
          <span>
            Resolved <When iso={a.resolved_at} />
          </span>
        )}
        <button
          type="button"
          className={m2.summaryButton}
          aria-expanded={open}
          aria-controls={`${id}-details`}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "Hide details" : "Details"}
        </button>
      </div>
      {error && (
        <p className={connect.inlineError} role="alert">
          {error}
        </p>
      )}
      {open && (
        <div className={m2.detailsBox} id={`${id}-details`}>
          <dl>
            <dt>Subject</dt>
            <dd className={m2.mono}>{a.subject}</dd>
            {Object.entries(a.details ?? {}).map(([k, v]) => (
              <DetailRow key={k} name={k} value={v} />
            ))}
            <dt>Alert ID</dt>
            <dd className={m2.mono}>{a.id}</dd>
          </dl>
        </div>
      )}
    </li>
  );
}

function DetailRow({ name, value }: { name: string; value: unknown }) {
  const text =
    value === null || value === undefined
      ? "–"
      : typeof value === "object"
        ? JSON.stringify(value)
        : String(value);
  return (
    <>
      <dt>{name.replace(/_/g, " ")}</dt>
      <dd className={typeof value === "object" && value !== null ? m2.mono : undefined}>{text}</dd>
    </>
  );
}
