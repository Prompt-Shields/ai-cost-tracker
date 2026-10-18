"use client";

import { useMemo, useState, type FormEvent } from "react";
import type { Deployment } from "@/lib/api";
import { matchSubject, type AllocApi, type SplitAllocation, type SplitRule } from "@/lib/api-m2-alloc";
import { EmptyNotice, errorMessage, ui } from "../ui";
import m2 from "./m2.module.css";

interface Props {
  alloc: AllocApi;
  admin: boolean;
  splits: SplitRule[];
  deployments: Deployment[];
  onChanged: () => void;
}

interface RowDraft {
  cost_centre: string;
  team: string;
  app: string;
  percent: string;
}

interface Draft {
  /** "<resource_id>|<deployment>"; fixed when editing an existing split. */
  target: string;
  existing: boolean;
  rows: RowDraft[];
}

const blankRow = (percent = ""): RowDraft => ({ cost_centre: "", team: "", app: "", percent });
const keyOf = (resourceId: string, deployment: string) => `${resourceId}|${deployment}`;

/** Sum in hundredths so 33.33 + 33.33 + 33.34 is exactly 100. */
function sumPercent(rows: RowDraft[]): number {
  return rows.reduce((s, r) => s + Math.round((Number(r.percent) || 0) * 100), 0) / 100;
}

function label(a: { cost_centre: string | null; team: string | null; app: string | null }): string {
  return [a.cost_centre, a.team, a.app].map((v) => v || "—").join(" · ");
}

export function SplitsSection({ alloc, admin, splits, deployments, onChanged }: Props) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const byKey = useMemo(
    () => new Map(deployments.map((d) => [keyOf(d.resource_id, d.name), d])),
    [deployments],
  );
  const taken = useMemo(() => new Set(splits.map((s) => keyOf(s.resource_id, s.deployment))), [splits]);

  const sum = draft ? sumPercent(draft.rows) : 0;
  const rowsOk =
    !!draft &&
    draft.rows.length > 0 &&
    draft.rows.every((r) => Number(r.percent) > 0 && (r.cost_centre.trim() || r.team.trim() || r.app.trim()));
  const canSave = !!draft && !!draft.target && rowsOk && sum === 100 && !busy;

  function setRow(i: number, patch: Partial<RowDraft>) {
    if (!draft) return;
    setDraft({ ...draft, rows: draft.rows.map((r, j) => (j === i ? { ...r, ...patch } : r)) });
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!draft || !canSave) return;
    const sep = draft.target.lastIndexOf("|");
    const resourceId = draft.target.slice(0, sep);
    const deployment = draft.target.slice(sep + 1);
    const allocations: SplitAllocation[] = draft.rows.map((r) => ({
      cost_centre: r.cost_centre.trim() || null,
      team: r.team.trim() || null,
      app: r.app.trim() || null,
      percent: Number(r.percent),
    }));
    setBusy(true);
    setError(null);
    try {
      await alloc.putSplit(resourceId, deployment, allocations);
      setDraft(null);
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(s: SplitRule) {
    if (!window.confirm(`Remove the split on ${s.deployment}? Its cost goes back to allocation rules.`)) return;
    try {
      await alloc.deleteSplit(s.resource_id, s.deployment);
      onChanged();
    } catch (err) {
      window.alert(errorMessage(err));
    }
  }

  const sortedDeployments = [...deployments].sort((a, b) => matchSubject(a).localeCompare(matchSubject(b)));

  return (
    <section className={ui.panel} aria-labelledby="splits-title">
      <div className={ui.panelHead}>
        <h2 id="splits-title" className={ui.panelTitle}>
          Split rules
        </h2>
        {admin && !draft && (
          <button
            type="button"
            className={m2.primary}
            onClick={() => {
              setError(null);
              setDraft({ target: "", existing: false, rows: [blankRow("50"), blankRow("50")] });
            }}
          >
            Add split
          </button>
        )}
      </div>
      <p className={ui.panelSub}>
        Share one deployment&apos;s cost between several owners by fixed percentages. Split cost is
        shown as approximate.
      </p>

      {draft && (
        <form className={m2.form} onSubmit={save} noValidate>
          <h3 className={m2.formTitle}>{draft.existing ? "Edit split" : "New split"}</h3>
          <label className={m2.field} style={{ maxWidth: 420 }}>
            Deployment
            <select
              value={draft.target}
              disabled={draft.existing}
              onChange={(e) => setDraft({ ...draft, target: e.target.value })}
            >
              <option value="">Choose a deployment…</option>
              {sortedDeployments.map((d) => {
                const k = keyOf(d.resource_id, d.name);
                return (
                  <option key={d.id} value={k} disabled={!draft.existing && taken.has(k)}>
                    {matchSubject(d)}
                    {taken.has(k) && !draft.existing ? " (has a split)" : ""}
                  </option>
                );
              })}
              {draft.existing && !byKey.has(draft.target) && (
                <option value={draft.target}>{draft.target.split("|").pop()}</option>
              )}
            </select>
          </label>

          <fieldset className={m2.fieldset}>
            <legend className={m2.legendText}>Shares</legend>
            {draft.rows.map((r, i) => (
              <div key={i} className={m2.splitRow}>
                <label className={m2.field}>
                  Cost centre
                  <input value={r.cost_centre} onChange={(e) => setRow(i, { cost_centre: e.target.value })} />
                </label>
                <label className={m2.field}>
                  Team
                  <input value={r.team} onChange={(e) => setRow(i, { team: e.target.value })} />
                </label>
                <label className={m2.field}>
                  App
                  <input value={r.app} onChange={(e) => setRow(i, { app: e.target.value })} />
                </label>
                <label className={m2.field}>
                  Percent
                  <input
                    type="number"
                    min={0}
                    max={100}
                    step="0.01"
                    inputMode="decimal"
                    value={r.percent}
                    onChange={(e) => setRow(i, { percent: e.target.value })}
                  />
                </label>
                <button
                  type="button"
                  className={`${m2.secondary} ${m2.small}`}
                  aria-label={`Remove share ${i + 1}`}
                  disabled={draft.rows.length <= 1}
                  onClick={() => setDraft({ ...draft, rows: draft.rows.filter((_, j) => j !== i) })}
                  style={{ marginBottom: 4 }}
                >
                  Remove
                </button>
              </div>
            ))}
            <div className={m2.actions}>
              <button
                type="button"
                className={`${m2.secondary} ${m2.small}`}
                onClick={() => setDraft({ ...draft, rows: [...draft.rows, blankRow()] })}
              >
                Add share
              </button>
              <span aria-live="polite">
                Total:{" "}
                <span className={`${m2.sum} ${sum === 100 ? "" : m2.sumBad}`}>{sum}%</span>
                {sum !== 100 && (
                  <span className={ui.cellSub}>
                    {" "}
                    ({sum < 100 ? `${(100 - sum).toFixed(2).replace(/\.?0+$/, "")}% left` : "over 100%"})
                  </span>
                )}
              </span>
            </div>
          </fieldset>

          {!rowsOk && (
            <p className={ui.cellSub} style={{ marginTop: 8 }}>
              Each share needs a percentage above 0 and at least one of cost centre, team or app.
            </p>
          )}
          {error && (
            <p className={m2.error} role="alert">
              {error}
            </p>
          )}
          <div className={m2.actions} style={{ marginTop: 16 }}>
            <button type="submit" className={m2.primary} disabled={!canSave}>
              {busy ? "Saving…" : "Save split"}
            </button>
            <button
              type="button"
              className={m2.secondary}
              disabled={busy}
              onClick={() => {
                setDraft(null);
                setError(null);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {splits.length === 0 ? (
        <EmptyNotice title="No split rules">
          Use a split when several apps share one deployment.
        </EmptyNotice>
      ) : (
        <div className={ui.tableWrap}>
          <table className={ui.table}>
            <thead>
              <tr>
                <th scope="col">Deployment</th>
                <th scope="col">Shares</th>
                {admin && (
                  <th scope="col" style={{ position: "relative" }}>
                    <span className={ui.srOnly}>Actions</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {splits.map((s) => {
                const d = byKey.get(keyOf(s.resource_id, s.deployment));
                return (
                  <tr key={keyOf(s.resource_id, s.deployment)}>
                    <th scope="row">
                      {s.deployment}
                      <div className={ui.cellSub}>{d ? matchSubject(d) : s.resource_id}</div>
                    </th>
                    <td className={m2.wrapCell}>
                      {s.allocations.map((a, i) => (
                        <div key={i}>
                          <span className={ui.num} style={{ display: "inline-block", minWidth: 48 }}>
                            {a.percent}%
                          </span>{" "}
                          {label(a)}
                        </div>
                      ))}
                    </td>
                    {admin && (
                      <td>
                        <div className={m2.rowActions}>
                          <button
                            type="button"
                            className={`${m2.secondary} ${m2.small}`}
                            onClick={() => {
                              setError(null);
                              setDraft({
                                target: keyOf(s.resource_id, s.deployment),
                                existing: true,
                                rows: s.allocations.map((a) => ({
                                  cost_centre: a.cost_centre ?? "",
                                  team: a.team ?? "",
                                  app: a.app ?? "",
                                  percent: String(a.percent),
                                })),
                              });
                            }}
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            className={`${m2.danger} ${m2.small}`}
                            onClick={() => remove(s)}
                          >
                            Delete
                          </button>
                        </div>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
