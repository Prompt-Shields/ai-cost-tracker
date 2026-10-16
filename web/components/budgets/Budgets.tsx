"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Deployment } from "@/lib/api";
import type { Budget, BudgetInput, CloudAccount } from "@/lib/api-m2-alerts";
import { formatMoney } from "@/lib/format";
import { useSession } from "../AuthProvider";
import { errorMessage } from "../connect/format";
import { useIsAdmin, usePoll } from "../connect/hooks";
import connect from "../connect/connect.module.css";
import { ui } from "../ui";
import { Pill, m2, useAlertsApi, type Tone } from "../alerts/shared";
import { BudgetForm, type Pickers } from "./BudgetForm";
import { percentUsed, scopeLabel, thresholdPercents } from "./format";

const STATUS: Record<Budget["status"], { tone: Tone; label: string }> = {
  ok: { tone: "ok", label: "On track" },
  warning: { tone: "warning", label: "Near limit" },
  over: { tone: "critical", label: "Over" },
};

function money(value: string | null, currency: string): string {
  if (value === null || value === undefined || value === "") return "–";
  const n = Number(value);
  return Number.isFinite(n) ? formatMoney(n, currency) : value;
}

/** Worst first, then by % used, so what needs attention is at the top. */
function sortBudgets(rows: Budget[]): Budget[] {
  const rank = { over: 0, warning: 1, ok: 2 } as const;
  return [...rows].sort(
    (a, b) =>
      Number(b.active) - Number(a.active) ||
      (rank[a.status] ?? 3) - (rank[b.status] ?? 3) ||
      (percentUsed(b) ?? 0) - (percentUsed(a) ?? 0) ||
      a.name.localeCompare(b.name),
  );
}

export function Budgets() {
  const api = useAlertsApi();
  const { api: core } = useSession();
  const isAdmin = useIsAdmin() === true;
  const load = useCallback(() => api.budgets().then(sortBudgets), [api]);
  const { state, reload } = usePoll(load, null);
  const [editing, setEditing] = useState<Budget | "new" | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [pickers, setPickers] = useState<Pickers>({ accounts: null, deployments: null });
  const formRef = useRef<HTMLDivElement>(null);

  // Pickers feed the scope helpers and the subscription names in the table; failures just
  // fall back to free-text inputs and raw ids.
  useEffect(() => {
    let cancelled = false;
    api
      .accounts()
      .then((accounts) => !cancelled && setPickers((p) => ({ ...p, accounts })))
      .catch(() => {});
    core
      .deployments()
      .then((deployments) => !cancelled && setPickers((p) => ({ ...p, deployments })))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [api, core]);

  const accountMap = useMemo(
    () => new Map<string, CloudAccount>((pickers.accounts ?? []).map((a) => [a.account_id, a])),
    [pickers.accounts],
  );
  const deploymentMap = useMemo(
    () => new Map<string, Deployment>((pickers.deployments ?? []).map((d) => [d.id, d])),
    [pickers.deployments],
  );

  useEffect(() => {
    if (editing) formRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [editing]);

  const save = async (input: BudgetInput) => {
    if (editing && editing !== "new") await api.updateBudget(editing.id, input);
    else await api.createBudget(input);
    setNote(`Saved “${input.name}”.`);
    setEditing(null);
    reload();
  };

  const remove = async (b: Budget) => {
    if (!window.confirm(`Delete the budget “${b.name}”? Its open alerts stop updating.`)) return;
    setBusyId(b.id);
    setRowError(null);
    try {
      await api.deleteBudget(b.id);
      setNote(`Deleted “${b.name}”.`);
      if (editing !== "new" && editing?.id === b.id) setEditing(null);
      reload();
    } catch (err) {
      setRowError(`Couldn't delete “${b.name}”: ${errorMessage(err)}`);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <main className={ui.main}>
      <div className={ui.toolbar}>
        <div>
          <h1 className={ui.title}>Budgets</h1>
          <p className={m2.lede}>
            Monthly spend limits per subscription, deployment, model or tag. Alerts go to your
            notification channels when spend or the month-end forecast crosses a threshold.
          </p>
        </div>
        {isAdmin && editing === null && !(state.status === "ready" && state.data.length === 0) && (
          <button
            type="button"
            className={connect.primary}
            onClick={() => {
              setNote(null);
              setEditing("new");
            }}
          >
            New budget
          </button>
        )}
      </div>

      {note && (
        <p className={connect.refreshNote} role="status">
          {note}
        </p>
      )}

      {isAdmin && editing !== null && (
        <div ref={formRef} style={{ scrollMarginTop: 16 }}>
          <BudgetForm
            budget={editing === "new" ? null : editing}
            pickers={pickers}
            onSave={save}
            onCancel={() => setEditing(null)}
          />
        </div>
      )}

      {state.status === "loading" && (
        <div className={`${ui.panel} ${ui.skeleton}`} style={{ height: 240 }} aria-busy="true">
          <span className={ui.srOnly}>Loading budgets…</span>
        </div>
      )}
      {state.status === "error" && (
        <div className={ui.notice} role="alert">
          <p className={ui.noticeTitle}>Couldn&apos;t load budgets</p>
          <p className={ui.noticeBody}>{state.message}</p>
          <button type="button" className={ui.retry} onClick={reload}>
            Try again
          </button>
        </div>
      )}
      {state.status === "ready" && state.data.length === 0 && editing === null && (
        <div className={ui.notice}>
          <p className={ui.noticeTitle}>No budgets yet</p>
          <p className={ui.noticeBody}>
            {isAdmin
              ? "Create one to get an alert before a subscription, team or model overspends."
              : "An admin can create budgets to get alerts before spend runs over."}
          </p>
          {isAdmin && (
            <div className={connect.actions} style={{ justifyContent: "center" }}>
              <button type="button" className={connect.primary} onClick={() => setEditing("new")}>
                New budget
              </button>
            </div>
          )}
        </div>
      )}
      {state.status === "ready" && state.data.length > 0 && (
        <section className={ui.panel} aria-label="Budgets">
          {state.refreshError && (
            <p className={connect.inlineError} role="alert" style={{ marginTop: 0 }}>
              Couldn&apos;t refresh: {state.refreshError}
            </p>
          )}
          {rowError && (
            <p className={connect.inlineError} role="alert" style={{ marginTop: 0, marginBottom: 8 }}>
              {rowError}
            </p>
          )}
          {/* relative: keeps the sr-only header inside the scroll box on phones */}
          <div className={ui.tableWrap} style={{ position: "relative" }}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Scope</th>
                  <th scope="col" className={ui.num}>
                    Amount
                  </th>
                  <th scope="col" className={ui.num}>
                    Month to date
                  </th>
                  <th scope="col" className={ui.num}>
                    Forecast
                  </th>
                  <th scope="col">Used</th>
                  <th scope="col">Status</th>
                  {isAdmin && (
                    <th scope="col">
                      <span className={ui.srOnly}>Actions</span>
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {state.data.map((b) => (
                  <BudgetRow
                    key={b.id}
                    budget={b}
                    accounts={accountMap}
                    deployments={deploymentMap}
                    isAdmin={isAdmin}
                    busy={busyId === b.id}
                    onEdit={() => {
                      setNote(null);
                      setEditing(b);
                    }}
                    onDelete={() => void remove(b)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </main>
  );
}

function BudgetRow({
  budget: b,
  accounts,
  deployments,
  isAdmin,
  busy,
  onEdit,
  onDelete,
}: {
  budget: Budget;
  accounts: Map<string, CloudAccount>;
  deployments: Map<string, Deployment>;
  isAdmin: boolean;
  busy: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const scope = scopeLabel(b, accounts, deployments);
  const pct = percentUsed(b);
  const status = b.active ? STATUS[b.status] ?? { tone: "off" as Tone, label: b.status } : { tone: "off" as Tone, label: "Inactive" };
  const forecastOver = b.forecast_month_end !== null && Number(b.forecast_month_end) > Number(b.amount);
  return (
    <tr>
      <th scope="row" style={{ whiteSpace: "normal", minWidth: 140 }}>
        {b.name}
      </th>
      <td style={{ whiteSpace: "normal", minWidth: 120 }}>
        <div>{scope.kind}</div>
        {scope.value && (
          <div className={ui.cellSub} title={b.scope_value ?? undefined} style={{ overflowWrap: "anywhere" }}>
            {scope.value}
          </div>
        )}
      </td>
      <td className={ui.num}>{money(b.amount, b.currency)}</td>
      <td className={ui.num}>{money(b.month_to_date, b.currency)}</td>
      <td className={ui.num}>
        {money(b.forecast_month_end, b.currency)}
        {forecastOver && <div className={ui.cellSub}>over by month end</div>}
      </td>
      <td>
        <UsageBar pct={pct} thresholds={thresholdPercents(b.thresholds)} status={b.status} />
      </td>
      <td>
        <Pill tone={status.tone} label={status.label} />
      </td>
      {isAdmin && (
        <td>
          <div className={m2.rowActions}>
            <button type="button" className={m2.iconButton} onClick={onEdit} disabled={busy}>
              Edit
            </button>
            <button
              type="button"
              className={`${m2.iconButton} ${m2.danger}`}
              onClick={onDelete}
              disabled={busy}
            >
              {busy ? "Deleting…" : "Delete"}
            </button>
          </div>
        </td>
      )}
    </tr>
  );
}

function UsageBar({
  pct,
  thresholds,
  status,
}: {
  pct: number | null;
  thresholds: number[];
  status: Budget["status"];
}) {
  if (pct === null) return <span className={m2.dim}>–</span>;
  // The bar's scale stretches past 100% when spend is over, so the overrun stays visible.
  const scale = Math.max(100, pct, ...thresholds);
  const tone = status === "over" ? m2.critical : status === "warning" ? m2.warning : "";
  const label = `${Math.round(pct)}% used`;
  return (
    <div className={m2.usage}>
      <div
        className={m2.bar}
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={scale}
        aria-valuenow={Math.round(pct)}
        title={thresholds.length ? `Alerts at ${thresholds.map((t) => `${t}%`).join(", ")}` : undefined}
      >
        <div className={`${m2.barFill} ${tone}`} style={{ width: `${Math.min(100, (pct / scale) * 100)}%` }} />
        {thresholds.map((t) => (
          <span key={t} className={m2.tick} style={{ left: `calc(${(t / scale) * 100}% - 1px)` }} />
        ))}
      </div>
      <span className={m2.usagePct}>{Math.round(pct)}%</span>
    </div>
  );
}
