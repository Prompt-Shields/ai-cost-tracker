"use client";

import { useMemo, useState, type FormEvent } from "react";
import type { Deployment } from "@/lib/api";
import {
  firstMatch,
  matchSubject,
  orderedRules,
  ruleMatches,
  type AllocApi,
  type AllocationRule,
  type AllocationRuleInput,
  type MatchType,
  type SplitRule,
} from "@/lib/api-m2-alloc";
import { EmptyNotice, errorMessage, ui } from "../ui";
import m2 from "./m2.module.css";

const MATCH_LABEL: Record<MatchType, string> = {
  tag: "Tag",
  name_pattern: "Name pattern",
  account: "Subscription",
};

const EMPTY: AllocationRuleInput = {
  name: "",
  priority: 100,
  match_type: "tag",
  match_key: "cost-centre",
  match_value: "",
  cost_centre: "",
  team: "",
  app: "",
  active: true,
};

type Draft = AllocationRuleInput & { id?: string };

interface Props {
  alloc: AllocApi;
  admin: boolean;
  rules: AllocationRule[];
  deployments: Deployment[];
  splits: SplitRule[];
  onChanged: () => void;
}

export function accountsOf(deployments: Deployment[]): { id: string; name: string }[] {
  const m = new Map<string, string>();
  for (const d of deployments) m.set(d.account_id, d.account_name || d.account_id);
  return [...m.entries()].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name));
}

function outputs(r: AllocationRuleInput): string {
  return [r.cost_centre, r.team, r.app].map((v) => v || "—").join(" · ");
}

export function RulesSection({ alloc, admin, rules, deployments, splits, onChanged }: Props) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const accountName = useMemo(() => new Map(accountsOf(deployments).map((a) => [a.id, a.name])), [deployments]);
  const splitKeys = useMemo(
    () => new Set(splits.filter((s) => s.active !== false).map((s) => `${s.resource_id}|${s.deployment}`)),
    [splits],
  );
  const sorted = useMemo(
    () => [...rules].sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name)),
    [rules],
  );
  // Which rule wins each deployment today (ignoring splits, which override at resolve time).
  const winners = useMemo(() => {
    const m = new Map<string, AllocationRule | null>();
    for (const d of deployments) m.set(d.id, firstMatch(rules, d));
    return m;
  }, [rules, deployments]);
  const wins = (id: string) => [...winners.values()].filter((r) => r?.id === id).length;

  function describe(r: AllocationRuleInput) {
    if (r.match_type === "tag") return <span className={m2.code}>{`${r.match_key}=${r.match_value}`}</span>;
    if (r.match_type === "name_pattern") return <span className={m2.code}>{r.match_value}</span>;
    return <span>{accountName.get(r.match_value) ?? r.match_value}</span>;
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!draft) return;
    const problem = validate(draft);
    if (problem) {
      setError(problem);
      return;
    }
    const { id, ...body } = draft;
    const clean: AllocationRuleInput = {
      ...body,
      name: body.name.trim(),
      match_key: body.match_type === "tag" ? (body.match_key ?? "").trim() : null,
      match_value: body.match_value.trim(),
      cost_centre: body.cost_centre?.trim() || null,
      team: body.team?.trim() || null,
      app: body.app?.trim() || null,
    };
    setBusy(true);
    setError(null);
    try {
      if (id) await alloc.updateRule(id, clean);
      else await alloc.createRule(clean);
      setDraft(null);
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(r: AllocationRule) {
    if (!window.confirm(`Delete rule “${r.name}”? Matching deployments fall through to the next rule.`)) return;
    try {
      await alloc.deleteRule(r.id);
      onChanged();
    } catch (err) {
      window.alert(errorMessage(err));
    }
  }

  return (
    <>
      <section className={ui.panel} aria-labelledby="rules-title">
        <div className={ui.panelHead}>
          <h2 id="rules-title" className={ui.panelTitle}>
            Allocation rules
          </h2>
          {admin && !draft && (
            <button
              type="button"
              className={m2.primary}
              onClick={() => {
                setError(null);
                const next = sorted.length ? sorted[sorted.length - 1].priority + 10 : 100;
                setDraft({ ...EMPTY, priority: next });
              }}
            >
              Add rule
            </button>
          )}
        </div>

        {draft && (
          <RuleEditor
            draft={draft}
            setDraft={setDraft}
            rules={rules}
            deployments={deployments}
            busy={busy}
            error={error}
            onSubmit={save}
            onCancel={() => {
              setDraft(null);
              setError(null);
            }}
          />
        )}

        {sorted.length === 0 ? (
          <EmptyNotice title="No allocation rules yet">
            Without rules all spend shows as unallocated.{" "}
            {admin ? "Add a rule that matches a tag such as cost-centre." : "Ask an admin to add rules."}
          </EmptyNotice>
        ) : (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th scope="col" className={ui.num}>
                    Priority
                  </th>
                  <th scope="col">Rule</th>
                  <th scope="col">Match</th>
                  <th scope="col">Cost centre · Team · App</th>
                  <th scope="col" className={ui.num}>
                    Deployments
                  </th>
                  {admin && (
                    <th scope="col" style={{ position: "relative" }}>
                      <span className={ui.srOnly}>Actions</span>
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {sorted.map((r) => (
                  <tr key={r.id} className={r.active ? undefined : m2.inactive}>
                    <td className={ui.num}>{r.priority}</td>
                    <th scope="row">
                      {r.name}
                      {!r.active && <span className={ui.badge}>inactive</span>}
                    </th>
                    <td>
                      <span className={ui.cellSub}>{MATCH_LABEL[r.match_type]}: </span>
                      {describe(r)}
                    </td>
                    <td>{outputs(r)}</td>
                    <td className={ui.num} title="Deployments this rule wins (first match)">
                      {r.active ? wins(r.id) : "—"}
                    </td>
                    {admin && (
                      <td>
                        <div className={m2.rowActions}>
                          <button
                            type="button"
                            className={`${m2.secondary} ${m2.small}`}
                            onClick={() => {
                              setError(null);
                              setDraft({ ...r });
                            }}
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            className={`${m2.danger} ${m2.small}`}
                            onClick={() => remove(r)}
                          >
                            Delete
                          </button>
                        </div>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className={ui.panel} aria-labelledby="test-title">
        <h2 id="test-title" className={ui.panelTitle}>
          Test against deployments
        </h2>
        <p className={ui.panelSub}>
          Which rule each deployment resolves to: active rules in priority order, first match wins.
        </p>
        {deployments.length === 0 ? (
          <p className={ui.muted}>No deployments in the inventory yet.</p>
        ) : (
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th scope="col">Deployment</th>
                  <th scope="col">Resolved by</th>
                  <th scope="col">Cost centre · Team · App</th>
                </tr>
              </thead>
              <tbody>
                {[...deployments]
                  .sort((a, b) => matchSubject(a).localeCompare(matchSubject(b)))
                  .map((d) => {
                    const r = winners.get(d.id) ?? null;
                    const split = splitKeys.has(`${d.resource_id}|${d.name}`);
                    return (
                      <tr key={d.id}>
                        <th scope="row">
                          {d.name}
                          <div className={ui.cellSub}>{matchSubject(d)}</div>
                        </th>
                        <td>
                          {split ? (
                            <span>
                              Split rule<span className={ui.badge}>approx.</span>
                            </span>
                          ) : r ? (
                            r.name
                          ) : (
                            <span className={ui.cellSub}>No match: unallocated</span>
                          )}
                        </td>
                        <td>{split ? "By percentage" : r ? outputs(r) : "—"}</td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function validate(d: Draft): string | null {
  if (!d.name.trim()) return "Give the rule a name.";
  if (!Number.isInteger(d.priority)) return "Priority must be a whole number.";
  if (d.match_type === "tag" && !(d.match_key ?? "").trim()) return "Enter the tag key to match.";
  if (!d.match_value.trim()) {
    return d.match_type === "account" ? "Pick a subscription." : "Enter the value to match.";
  }
  if (!d.cost_centre?.trim() && !d.team?.trim() && !d.app?.trim()) {
    return "Set at least one of cost centre, team or app.";
  }
  return null;
}

function RuleEditor({
  draft,
  setDraft,
  rules,
  deployments,
  busy,
  error,
  onSubmit,
  onCancel,
}: {
  draft: Draft;
  setDraft: (d: Draft) => void;
  rules: AllocationRule[];
  deployments: Deployment[];
  busy: boolean;
  error: string | null;
  onSubmit: (e: FormEvent) => void;
  onCancel: () => void;
}) {
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft({ ...draft, [k]: v });
  const accounts = accountsOf(deployments);
  const tagKeys = useMemo(
    () => [...new Set(deployments.flatMap((d) => Object.keys(d.tags)))].sort(),
    [deployments],
  );

  // Preview: what the draft matches, and whether a higher-priority rule takes it first.
  const preview = useMemo(() => {
    const complete =
      draft.match_value.trim() && (draft.match_type !== "tag" || (draft.match_key ?? "").trim());
    if (!complete) return null;
    const self = { ...draft, id: draft.id ?? "__draft__", active: true } as AllocationRule;
    const others = rules.filter((r) => r.id !== draft.id);
    const order = orderedRules([...others, self]);
    return deployments
      .filter((d) => ruleMatches(self, d))
      .map((d) => {
        const winner = order.find((r) => ruleMatches(r, d)) ?? null;
        return { d, takenBy: winner && winner.id !== self.id ? winner.name : null };
      })
      .sort((a, b) => a.d.name.localeCompare(b.d.name));
  }, [draft, rules, deployments]);

  return (
    <form className={m2.form} onSubmit={onSubmit} noValidate>
      <h3 className={m2.formTitle}>{draft.id ? `Edit “${draft.name}”` : "New rule"}</h3>
      <div className={m2.grid}>
        <label className={m2.field}>
          Name
          <input value={draft.name} onChange={(e) => set("name", e.target.value)} required />
        </label>
        <label className={m2.field}>
          Priority
          <input
            type="number"
            step={1}
            value={Number.isNaN(draft.priority) ? "" : draft.priority}
            onChange={(e) => set("priority", e.target.valueAsNumber)}
          />
          <span className={m2.hint}>Lower runs first</span>
        </label>
        <label className={m2.field}>
          Match by
          <select
            value={draft.match_type}
            onChange={(e) => {
              const t = e.target.value as MatchType;
              setDraft({ ...draft, match_type: t, match_value: "", match_key: t === "tag" ? "cost-centre" : null });
            }}
          >
            <option value="tag">Tag</option>
            <option value="name_pattern">Name pattern (glob)</option>
            <option value="account">Subscription</option>
          </select>
        </label>

        {draft.match_type === "tag" && (
          <>
            <label className={m2.field}>
              Tag key
              <input
                list="alloc-tag-keys"
                value={draft.match_key ?? ""}
                onChange={(e) => set("match_key", e.target.value)}
              />
              <datalist id="alloc-tag-keys">
                {tagKeys.map((k) => (
                  <option key={k} value={k} />
                ))}
              </datalist>
            </label>
            <label className={m2.field}>
              Tag value
              <input value={draft.match_value} onChange={(e) => set("match_value", e.target.value)} />
              <span className={m2.hint}>Exact, case-sensitive</span>
            </label>
          </>
        )}
        {draft.match_type === "name_pattern" && (
          <label className={`${m2.field} ${m2.wide}`}>
            Pattern
            <input
              value={draft.match_value}
              placeholder="aoai-search-*/gpt-4o*"
              onChange={(e) => set("match_value", e.target.value)}
            />
            <span className={m2.hint}>
              Matched against <span className={m2.code}>account/deployment</span>; * any text, ? one
              character. Example: <span className={m2.code}>aoai-prod-*/*-chat</span>
            </span>
          </label>
        )}
        {draft.match_type === "account" && (
          <label className={m2.field}>
            Subscription
            <select value={draft.match_value} onChange={(e) => set("match_value", e.target.value)}>
              <option value="">Choose…</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
              {draft.match_value && !accounts.some((a) => a.id === draft.match_value) && (
                <option value={draft.match_value}>{draft.match_value}</option>
              )}
            </select>
          </label>
        )}
      </div>

      <fieldset className={m2.fieldset}>
        <legend className={m2.legendText}>Allocate to</legend>
        <div className={m2.grid}>
          <label className={m2.field}>
            Cost centre
            <input value={draft.cost_centre ?? ""} onChange={(e) => set("cost_centre", e.target.value)} />
          </label>
          <label className={m2.field}>
            Team
            <input value={draft.team ?? ""} onChange={(e) => set("team", e.target.value)} />
          </label>
          <label className={m2.field}>
            App
            <input value={draft.app ?? ""} onChange={(e) => set("app", e.target.value)} />
          </label>
          <label className={m2.check} style={{ alignSelf: "end", paddingBottom: 6 }}>
            <input type="checkbox" checked={draft.active} onChange={(e) => set("active", e.target.checked)} />
            Active
          </label>
        </div>
      </fieldset>

      <div className={m2.preview} aria-live="polite">
        <p className={m2.previewTitle}>
          {preview === null
            ? "Fill in the match to preview deployments"
            : `Matches ${preview.length} deployment${preview.length === 1 ? "" : "s"}` +
              (preview.some((p) => p.takenBy)
                ? ` (${preview.filter((p) => !p.takenBy).length} after higher-priority rules)`
                : "")}
        </p>
        {preview && preview.length > 0 && (
          <div className={ui.chips}>
            {preview.map(({ d, takenBy }) => (
              <span
                key={d.id}
                className={ui.chip}
                style={takenBy ? { textDecoration: "line-through" } : undefined}
                title={takenBy ? `Taken first by “${takenBy}”` : matchSubject(d)}
              >
                {d.name}
                {takenBy ? ` (→ ${takenBy})` : ""}
              </span>
            ))}
          </div>
        )}
      </div>

      {error && (
        <p className={m2.error} role="alert">
          {error}
        </p>
      )}
      <div className={m2.actions} style={{ marginTop: 16 }}>
        <button type="submit" className={m2.primary} disabled={busy}>
          {busy ? "Saving…" : draft.id ? "Save rule" : "Create rule"}
        </button>
        <button type="button" className={m2.secondary} onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}
