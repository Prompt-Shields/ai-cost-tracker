"use client";

import { Fragment, useCallback, useEffect, useMemo, useState, type ChangeEvent } from "react";
import { ApiError } from "@/lib/api";
import { AdminApi, isAdmin, type AuditFilters, type AuditRow } from "@/lib/api-m2-admin";
import { useSession } from "../AuthProvider";
import styles from "../settings/admin.module.css";
import { formatWhen } from "../settings/format";
import { useMe } from "../settings/useMe";
import { EmptyNotice, ErrorNotice, Skeleton, errorMessage, ui } from "../ui";

const PAGE = 100;

// Suggestions only; the filter accepts any action string the API records.
const KNOWN_ACTIONS = [
  "login",
  "export.csv",
  "api_key.create",
  "api_key.revoke",
  "budget.create",
  "budget.update",
  "budget.delete",
  "connection.track",
  "notification_channel.create",
  "allocation_rule.create",
  "billing_period.update",
  "alert.resolve",
];

type State =
  | { status: "loading" }
  | { status: "forbidden" }
  | { status: "error"; message: string }
  | { status: "ready"; rows: AuditRow[]; next: string | null; more: "idle" | "loading" | string };

/** Who changed what (#37). Admin only; keyset-paged so old entries never shift. */
export function AuditLog() {
  const { me, loaded } = useMe();
  if (loaded && me && !isAdmin(me.roles)) return <Forbidden />;
  return <AuditBody />;
}

function Forbidden() {
  return (
    <main className={ui.main}>
      <h1 className={ui.title}>Audit log</h1>
      <EmptyNotice title="Admins only">
        The audit log shows sign-ins, exports and every settings change, so it&apos;s limited to
        admins. Ask an owner if you need access.
      </EmptyNotice>
    </main>
  );
}

function AuditBody() {
  const { api } = useSession();
  const admin = useMemo(() => new AdminApi(api.getToken), [api]);
  const [draft, setDraft] = useState<AuditFilters>({});
  const [filters, setFilters] = useState<AuditFilters>({});
  const [state, setState] = useState<State>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    admin
      .audit(filters, null, PAGE)
      .then((page) => {
        if (!cancelled)
          setState({ status: "ready", rows: page.rows, next: page.next_before_id, more: "idle" });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 403) setState({ status: "forbidden" });
        else setState({ status: "error", message: errorMessage(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [admin, filters, attempt]);

  const loadMore = useCallback(async () => {
    if (state.status !== "ready" || !state.next) return;
    const before = state.next;
    setState({ ...state, more: "loading" });
    try {
      const page = await admin.audit(filters, before, PAGE);
      setState((s) =>
        s.status === "ready"
          ? { status: "ready", rows: [...s.rows, ...page.rows], next: page.next_before_id, more: "idle" }
          : s,
      );
    } catch (err) {
      setState((s) => (s.status === "ready" ? { ...s, more: errorMessage(err) } : s));
    }
  }, [admin, filters, state]);

  if (state.status === "forbidden") return <Forbidden />;

  const set = (k: keyof AuditFilters) => (e: ChangeEvent<HTMLInputElement>) =>
    setDraft((d) => ({ ...d, [k]: e.target.value || undefined }));
  const active = Object.values(filters).some(Boolean);

  return (
    <main className={ui.main}>
      <div className={ui.toolbar}>
        <div>
          <h1 className={ui.title}>Audit log</h1>
          <p className={ui.subtitle}>Sign-ins, exports and settings changes, newest first.</p>
        </div>
      </div>

      <section className={ui.panel} aria-label="Filters">
        <form
          className={styles.form}
          onSubmit={(e) => {
            e.preventDefault();
            setFilters({ ...draft });
          }}
        >
          <label className={styles.field}>
            <span className={styles.fieldLabel}>Action</span>
            <input
              className={styles.text}
              list="audit-actions"
              value={draft.action ?? ""}
              onChange={set("action")}
              placeholder="e.g. api_key.revoke"
              autoComplete="off"
              spellCheck={false}
            />
            <datalist id="audit-actions">
              {KNOWN_ACTIONS.map((a) => (
                <option key={a} value={a} />
              ))}
            </datalist>
          </label>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>Actor</span>
            <input
              className={styles.text}
              value={draft.actor ?? ""}
              onChange={set("actor")}
              placeholder="Name or id"
              autoComplete="off"
            />
          </label>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>From</span>
            <input
              type="date"
              className={styles.text}
              value={draft.start ?? ""}
              max={draft.end}
              onChange={set("start")}
            />
          </label>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>To</span>
            <input
              type="date"
              className={styles.text}
              value={draft.end ?? ""}
              min={draft.start}
              onChange={set("end")}
            />
          </label>
          <div className={ui.controls}>
            <button type="submit" className={styles.primary}>
              Apply
            </button>
            {(active || Object.values(draft).some(Boolean)) && (
              <button
                type="button"
                className={styles.secondary}
                onClick={() => {
                  setDraft({});
                  setFilters({});
                }}
              >
                Clear
              </button>
            )}
          </div>
        </form>
      </section>

      {state.status === "loading" && <Skeleton height={360} label="Loading audit log…" />}
      {state.status === "error" && (
        <ErrorNotice
          title="Couldn't load the audit log"
          message={state.message}
          onRetry={() => setAttempt((a) => a + 1)}
        />
      )}
      {state.status === "ready" && state.rows.length === 0 && (
        <EmptyNotice title={active ? "No entries match these filters" : "No audit entries yet"}>
          {active ? "Try a wider date range or clear the filters." : "Changes and exports will be recorded here."}
        </EmptyNotice>
      )}
      {state.status === "ready" && state.rows.length > 0 && (
        <section className={ui.panel}>
          <AuditTable rows={state.rows} />
          {state.next && (
            <div className={styles.center}>
              <button
                type="button"
                className={styles.secondary}
                onClick={() => void loadMore()}
                disabled={state.more === "loading"}
              >
                {state.more === "loading" ? "Loading…" : "Load more"}
              </button>
            </div>
          )}
          {state.more !== "idle" && state.more !== "loading" && (
            <p className={styles.error} role="alert" style={{ textAlign: "center" }}>
              {state.more}
            </p>
          )}
        </section>
      )}
    </main>
  );
}

function hasDetails(details: unknown): boolean {
  if (details === null || details === undefined) return false;
  if (typeof details === "object") return Object.keys(details as object).length > 0;
  return true;
}

function AuditTable({ rows }: { rows: AuditRow[] }) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (id: string) =>
    setOpen((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  return (
    <div className={`${ui.tableWrap} ${styles.clip}`}>
      <table className={ui.table}>
        <thead>
          <tr>
            <th scope="col">When</th>
            <th scope="col">Actor</th>
            <th scope="col">Action</th>
            <th scope="col">Target</th>
            <th scope="col">
              <span className={ui.srOnly}>Details</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const expanded = open.has(r.id);
            const detailId = `audit-details-${r.id}`;
            return (
              <Fragment key={r.id}>
                <tr>
                  <td>{formatWhen(r.at)}</td>
                  <td title={r.actor_id}>
                    {r.actor_name ?? r.actor_id}
                    {r.actor_name && <div className={ui.cellSub}>{shortId(r.actor_id)}</div>}
                  </td>
                  <td>
                    <span className={styles.mono}>{r.action}</span>
                  </td>
                  <td title={r.target_id ?? undefined}>
                    {r.target_type ?? "—"}
                    {r.target_id && <div className={ui.cellSub}>{shortId(r.target_id)}</div>}
                  </td>
                  <td>
                    {hasDetails(r.details) && (
                      <button
                        type="button"
                        className={styles.linkButton}
                        aria-expanded={expanded}
                        aria-controls={detailId}
                        onClick={() => toggle(r.id)}
                      >
                        {expanded ? "Hide details" : "Details"}
                      </button>
                    )}
                  </td>
                </tr>
                {expanded && (
                  <tr className={styles.detailRow} id={detailId}>
                    <td colSpan={5}>
                      <pre className={styles.json}>{JSON.stringify(r.details, null, 2)}</pre>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Long ids (oids, resource ids) get their tail; the full value is in the title. */
function shortId(id: string): string {
  return id.length > 24 ? `…${id.slice(-20)}` : id;
}
