"use client";

import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import type { Deployment } from "@/lib/api";
import type { Budget, BudgetInput, BudgetScopeType, CloudAccount } from "@/lib/api-m2-alerts";
import { errorMessage } from "../connect/format";
import connect from "../connect/connect.module.css";
import { m2 } from "../alerts/shared";
import { SCOPE_TYPES, deploymentKey, parseThresholds, thresholdPercents } from "./format";

const CURRENCIES = ["USD", "EUR", "GBP"];
const AMOUNT_RE = /^\d+(\.\d{1,2})?$/;

export interface Pickers {
  accounts: CloudAccount[] | null; // null: not loaded / failed, fall back to free text
  deployments: Deployment[] | null;
}

interface Draft {
  name: string;
  amount: string;
  currency: string;
  scopeType: BudgetScopeType;
  scopeValue: string;
  tagKey: string;
  tagValue: string;
  thresholds: string;
  active: boolean;
}

function toDraft(b: Budget | null): Draft {
  const [tagKey = "", ...rest] = b?.scope_type === "tag" ? (b.scope_value ?? "").split(":") : [];
  return {
    name: b?.name ?? "",
    amount: b ? String(Number(b.amount)) : "",
    currency: b?.currency ?? "USD",
    scopeType: b?.scope_type ?? "all",
    scopeValue: b && b.scope_type !== "tag" ? (b.scope_value ?? "") : "",
    tagKey,
    tagValue: rest.join(":"),
    thresholds: b ? thresholdPercents(b.thresholds).join(", ") : "80, 100",
    active: b?.active ?? true,
  };
}

type Errors = Partial<Record<"name" | "amount" | "scope" | "thresholds", string>>;

function validate(d: Draft): { errors: Errors; input: BudgetInput | null } {
  const errors: Errors = {};
  if (!d.name.trim()) errors.name = "Give the budget a name.";
  const amount = d.amount.trim().replace(/,/g, "");
  if (!AMOUNT_RE.test(amount) || Number(amount) <= 0) {
    errors.amount = "Enter a monthly amount, e.g. 5000 or 1250.50.";
  }
  let scopeValue: string | null = null;
  if (d.scopeType === "tag") {
    if (!d.tagKey.trim() || !d.tagValue.trim()) errors.scope = "Enter both the tag key and value.";
    else if (d.tagKey.includes(":")) errors.scope = "The tag key can't contain a colon.";
    else scopeValue = `${d.tagKey.trim()}:${d.tagValue.trim()}`;
  } else if (d.scopeType !== "all") {
    if (!d.scopeValue.trim()) errors.scope = "Choose what this budget covers.";
    else scopeValue = d.scopeValue.trim();
  }
  const thresholds = parseThresholds(d.thresholds);
  if (thresholds === null) errors.thresholds = "Use percentages separated by commas, e.g. 80, 100.";
  if (Object.keys(errors).length > 0 || thresholds === null) return { errors, input: null };
  return {
    errors,
    input: {
      name: d.name.trim(),
      scope_type: d.scopeType,
      scope_value: scopeValue,
      amount,
      currency: d.currency,
      thresholds,
      active: d.active,
    },
  };
}

export function BudgetForm({
  budget,
  pickers,
  onSave,
  onCancel,
}: {
  budget: Budget | null;
  pickers: Pickers;
  onSave: (input: BudgetInput) => Promise<void>;
  onCancel: () => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState<Draft>(() => toDraft(budget));
  const [errors, setErrors] = useState<Errors>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setDraft(toDraft(budget));
    setErrors({});
    setSaveError(null);
    nameRef.current?.focus();
  }, [budget]);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  const models = useMemo(
    () =>
      [...new Set((pickers.deployments ?? []).map((d) => d.model).filter((m): m is string => !!m))].sort(),
    [pickers.deployments],
  );
  const tagKeys = useMemo(
    () => [...new Set((pickers.deployments ?? []).flatMap((d) => Object.keys(d.tags)))].sort(),
    [pickers.deployments],
  );
  const tagValues = useMemo(
    () =>
      [
        ...new Set(
          (pickers.deployments ?? [])
            .map((d) => d.tags[draft.tagKey.trim()])
            .filter((v): v is string => typeof v === "string" && v !== ""),
        ),
      ].sort(),
    [pickers.deployments, draft.tagKey],
  );
  const deploymentOptions = useMemo(
    () =>
      (pickers.deployments ?? [])
        .map((d) => ({ key: deploymentKey(d), d }))
        .sort((a, b) => a.key.localeCompare(b.key)),
    [pickers.deployments],
  );

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const { errors: errs, input } = validate(draft);
    setErrors(errs);
    if (!input) return;
    setSaving(true);
    setSaveError(null);
    try {
      await onSave(input);
    } catch (err) {
      setSaveError(errorMessage(err));
      setSaving(false);
    }
  };

  const scopeField = () => {
    const invalid = errors.scope ? true : undefined;
    const describedBy = `${id}-scope-help`;
    switch (draft.scopeType) {
      case "all":
        return (
          <p className={m2.help} id={describedBy}>
            Covers every tracked subscription and deployment.
          </p>
        );
      case "account": {
        const accounts = pickers.accounts ?? [];
        const known = accounts.some((a) => a.account_id === draft.scopeValue);
        return accounts.length > 0 ? (
          <select
            id={`${id}-scope`}
            className={m2.control}
            value={draft.scopeValue}
            aria-invalid={invalid}
            aria-describedby={describedBy}
            onChange={(e) => set("scopeValue", e.target.value)}
          >
            <option value="">Choose a subscription…</option>
            {!known && draft.scopeValue && <option value={draft.scopeValue}>{draft.scopeValue}</option>}
            {accounts.map((a) => (
              <option key={a.account_id} value={a.account_id}>
                {a.name ? `${a.name} (${a.account_id.slice(0, 8)}…)` : a.account_id}
              </option>
            ))}
          </select>
        ) : (
          <input
            id={`${id}-scope`}
            className={m2.control}
            value={draft.scopeValue}
            placeholder="Subscription ID"
            aria-invalid={invalid}
            aria-describedby={describedBy}
            onChange={(e) => set("scopeValue", e.target.value)}
          />
        );
      }
      case "deployment": {
        // The API identifies a deployment scope by deployment id; the label is the readable key.
        const known = deploymentOptions.some((o) => o.d.id === draft.scopeValue);
        return deploymentOptions.length > 0 ? (
          <select
            id={`${id}-scope`}
            className={m2.control}
            value={draft.scopeValue}
            aria-invalid={invalid}
            aria-describedby={describedBy}
            onChange={(e) => set("scopeValue", e.target.value)}
          >
            <option value="">Choose a deployment…</option>
            {!known && draft.scopeValue && <option value={draft.scopeValue}>{draft.scopeValue}</option>}
            {deploymentOptions.map(({ key, d }) => (
              <option key={d.id} value={d.id}>
                {key}
                {d.model ? ` · ${d.model}` : ""}
              </option>
            ))}
          </select>
        ) : (
          <input
            id={`${id}-scope`}
            className={m2.control}
            value={draft.scopeValue}
            placeholder="deployment id"
            aria-invalid={invalid}
            aria-describedby={describedBy}
            onChange={(e) => set("scopeValue", e.target.value)}
          />
        );
      }
      case "model":
        return (
          <>
            <input
              id={`${id}-scope`}
              className={m2.control}
              value={draft.scopeValue}
              list={`${id}-models`}
              placeholder="e.g. gpt-4o"
              aria-invalid={invalid}
              aria-describedby={describedBy}
              onChange={(e) => set("scopeValue", e.target.value)}
            />
            <datalist id={`${id}-models`}>
              {models.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </>
        );
      case "tag":
        return (
          <div className={m2.row}>
            <input
              id={`${id}-scope`}
              className={m2.control}
              value={draft.tagKey}
              list={`${id}-tagkeys`}
              placeholder="Key, e.g. team"
              aria-label="Tag key"
              aria-invalid={invalid}
              aria-describedby={describedBy}
              onChange={(e) => set("tagKey", e.target.value)}
            />
            <input
              className={m2.control}
              value={draft.tagValue}
              list={`${id}-tagvalues`}
              placeholder="Value, e.g. search"
              aria-label="Tag value"
              aria-invalid={invalid}
              aria-describedby={describedBy}
              onChange={(e) => set("tagValue", e.target.value)}
            />
            <datalist id={`${id}-tagkeys`}>
              {tagKeys.map((k) => (
                <option key={k} value={k} />
              ))}
            </datalist>
            <datalist id={`${id}-tagvalues`}>
              {tagValues.map((v) => (
                <option key={v} value={v} />
              ))}
            </datalist>
          </div>
        );
    }
  };

  const scopeHelp: Record<BudgetScopeType, string | null> = {
    all: null,
    account: "Spend on AI resources in this subscription.",
    deployment: "One deployment, as account resource / deployment name.",
    model: "Spend on this model across all deployments.",
    tag: "Deployments tagged key:value. Billed cost is per resource, so mixed-tag resources split approximately.",
  };

  return (
    <section className={connect.panel} aria-labelledby={`${id}-title`}>
      <h2 className={connect.panelTitle} id={`${id}-title`}>
        {budget ? `Edit “${budget.name}”` : "New budget"}
      </h2>
      <form className={m2.form} onSubmit={submit} noValidate>
        <div className={m2.field}>
          <label className={m2.label} htmlFor={`${id}-name`}>
            Name
          </label>
          <input
            ref={nameRef}
            id={`${id}-name`}
            className={m2.control}
            value={draft.name}
            maxLength={200}
            placeholder="e.g. Search team monthly"
            aria-invalid={errors.name ? true : undefined}
            onChange={(e) => set("name", e.target.value)}
          />
          {errors.name && <p className={m2.fieldError}>{errors.name}</p>}
        </div>

        <div className={m2.field}>
          <label className={m2.label} htmlFor={`${id}-amount`}>
            Monthly amount
          </label>
          <div className={m2.row}>
            <input
              id={`${id}-amount`}
              className={m2.control}
              value={draft.amount}
              inputMode="decimal"
              placeholder="5000"
              style={{ flex: "2 1 0" }}
              aria-invalid={errors.amount ? true : undefined}
              onChange={(e) => set("amount", e.target.value)}
            />
            <select
              className={m2.control}
              value={draft.currency}
              aria-label="Currency"
              onChange={(e) => set("currency", e.target.value)}
            >
              {[...new Set([...CURRENCIES, draft.currency])].map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </div>
          {errors.amount && <p className={m2.fieldError}>{errors.amount}</p>}
        </div>

        <div className={m2.field}>
          <label className={m2.label} htmlFor={`${id}-scope-type`}>
            Scope
          </label>
          <select
            id={`${id}-scope-type`}
            className={m2.control}
            value={draft.scopeType}
            onChange={(e) => {
              set("scopeType", e.target.value as BudgetScopeType);
              set("scopeValue", "");
            }}
          >
            {SCOPE_TYPES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </div>

        <div className={m2.field}>
          <label className={m2.label} htmlFor={`${id}-scope`}>
            {draft.scopeType === "tag" ? "Tag" : "Covers"}
          </label>
          {scopeField()}
          {draft.scopeType !== "all" && (
            <p className={m2.help} id={`${id}-scope-help`}>
              {scopeHelp[draft.scopeType]}
            </p>
          )}
          {errors.scope && <p className={m2.fieldError}>{errors.scope}</p>}
        </div>

        <div className={m2.field}>
          <label className={m2.label} htmlFor={`${id}-thresholds`}>
            Alert at (% of amount)
          </label>
          <input
            id={`${id}-thresholds`}
            className={m2.control}
            value={draft.thresholds}
            inputMode="decimal"
            placeholder="80, 100"
            aria-invalid={errors.thresholds ? true : undefined}
            aria-describedby={`${id}-thresholds-help`}
            onChange={(e) => set("thresholds", e.target.value)}
          />
          <p className={m2.help} id={`${id}-thresholds-help`}>
            Comma-separated. An alert fires when spend or the month-end forecast crosses each one.
          </p>
          {errors.thresholds && <p className={m2.fieldError}>{errors.thresholds}</p>}
        </div>

        <div className={m2.field} style={{ justifyContent: "center" }}>
          <label className={m2.check}>
            <input type="checkbox" checked={draft.active} onChange={(e) => set("active", e.target.checked)} />
            Active (inactive budgets raise no alerts)
          </label>
        </div>

        {saveError && (
          <p className={`${m2.fieldError} ${m2.full}`} role="alert" style={{ fontSize: 14 }}>
            Couldn&apos;t save: {saveError}
          </p>
        )}
        <div className={m2.formActions}>
          <button type="submit" className={connect.primary} disabled={saving}>
            {saving ? "Saving…" : budget ? "Save changes" : "Create budget"}
          </button>
          <button type="button" className={connect.secondary} onClick={onCancel} disabled={saving}>
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}
