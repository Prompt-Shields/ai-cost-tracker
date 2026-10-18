"use client";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import {
  KNOWN_ALERT_KINDS,
  SEVERITIES,
  alertKindLabel,
  type ChannelInput,
  type ChannelKind,
  type NotificationChannel,
  type Severity,
} from "@/lib/api-m2-alerts";
import { errorMessage } from "../connect/format";
import connect from "../connect/connect.module.css";
import { m2, severityLabel } from "../alerts/shared";
import { CHANNEL_KINDS, KIND_ORDER } from "./kinds";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

interface Draft {
  name: string;
  kind: ChannelKind;
  target: string;
  alertKinds: string[];
  minSeverity: Severity;
  digest: boolean;
  active: boolean;
}

function toDraft(c: NotificationChannel | null): Draft {
  return {
    name: c?.name ?? "",
    kind: c?.kind ?? "slack",
    target: "",
    alertKinds: c?.alert_kinds ?? [],
    minSeverity: c?.min_severity ?? "warning",
    digest: c?.digest ?? false,
    active: c?.active ?? true,
  };
}

type Errors = Partial<Record<"name" | "target", string>>;

function validateTarget(kind: ChannelKind, target: string, required: boolean): string | undefined {
  const t = target.trim();
  if (!t) return required ? `Enter the ${CHANNEL_KINDS[kind].targetLabel.toLowerCase()}.` : undefined;
  if (kind === "email") return EMAIL_RE.test(t) ? undefined : "That doesn't look like an email address.";
  try {
    const url = new URL(t);
    if (url.protocol !== "https:") return "Use an https:// URL.";
  } catch {
    return "That doesn't look like a URL.";
  }
  return undefined;
}

export function ChannelForm({
  channel,
  onSave,
  onCancel,
}: {
  channel: NotificationChannel | null;
  onSave: (input: ChannelInput) => Promise<void>;
  onCancel: () => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState<Draft>(() => toDraft(channel));
  const [errors, setErrors] = useState<Errors>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setDraft(toDraft(channel));
    setErrors({});
    setSaveError(null);
    nameRef.current?.focus();
  }, [channel]);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  const info = CHANNEL_KINDS[draft.kind];
  // Changing the kind of an existing channel needs a new target: the old one is the wrong shape.
  const targetRequired = !channel || channel.kind !== draft.kind;
  const kinds = [...new Set([...KNOWN_ALERT_KINDS, ...draft.alertKinds])];

  const toggleKind = (k: string, on: boolean) =>
    set("alertKinds", on ? [...draft.alertKinds, k] : draft.alertKinds.filter((x) => x !== k));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const errs: Errors = {};
    if (!draft.name.trim()) errs.name = "Give the channel a name.";
    const targetError = validateTarget(draft.kind, draft.target, targetRequired);
    if (targetError) errs.target = targetError;
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    const input: ChannelInput = {
      name: draft.name.trim(),
      kind: draft.kind,
      alert_kinds: draft.alertKinds,
      min_severity: draft.minSeverity,
      digest: draft.digest,
      active: draft.active,
    };
    if (draft.target.trim()) input.target = draft.target.trim();
    setSaving(true);
    setSaveError(null);
    try {
      await onSave(input);
    } catch (err) {
      setSaveError(errorMessage(err));
      setSaving(false);
    }
  };

  return (
    <section className={connect.panel} aria-labelledby={`${id}-title`}>
      <h2 className={connect.panelTitle} id={`${id}-title`}>
        {channel ? `Edit “${channel.name}”` : "New channel"}
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
            placeholder="e.g. #ai-spend"
            aria-invalid={errors.name ? true : undefined}
            onChange={(e) => set("name", e.target.value)}
          />
          {errors.name && <p className={m2.fieldError}>{errors.name}</p>}
        </div>

        <div className={m2.field}>
          <label className={m2.label} htmlFor={`${id}-kind`}>
            Type
          </label>
          <select
            id={`${id}-kind`}
            className={m2.control}
            value={draft.kind}
            onChange={(e) => set("kind", e.target.value as ChannelKind)}
          >
            {KIND_ORDER.map((k) => (
              <option key={k} value={k}>
                {CHANNEL_KINDS[k].label}
              </option>
            ))}
          </select>
        </div>

        <div className={`${m2.field} ${m2.full}`}>
          <label className={m2.label} htmlFor={`${id}-target`}>
            {info.targetLabel}
          </label>
          <input
            id={`${id}-target`}
            className={m2.control}
            type={info.inputType}
            value={draft.target}
            autoComplete="off"
            spellCheck={false}
            placeholder={
              targetRequired ? info.placeholder : `Leave blank to keep ${channel?.target_masked ?? "the current one"}`
            }
            aria-invalid={errors.target ? true : undefined}
            aria-describedby={`${id}-target-help`}
            onChange={(e) => set("target", e.target.value)}
          />
          <p className={m2.help} id={`${id}-target-help`}>
            {info.help}
            {info.inputType === "url" && " The URL contains a secret token, so it is never shown again in full."}
          </p>
          {errors.target && <p className={m2.fieldError}>{errors.target}</p>}
        </div>

        <fieldset className={`${m2.checks} ${m2.full}`}>
          <legend>Alert types</legend>
          {kinds.map((k) => (
            <label key={k} className={m2.check}>
              <input
                type="checkbox"
                checked={draft.alertKinds.includes(k)}
                onChange={(e) => toggleKind(k, e.target.checked)}
              />
              {alertKindLabel(k)}
            </label>
          ))}
          <p className={`${m2.help} ${m2.full}`} style={{ flexBasis: "100%" }}>
            {draft.alertKinds.length === 0
              ? "None selected: every alert type is sent here."
              : `Only ${draft.alertKinds.length} selected type${draft.alertKinds.length === 1 ? "" : "s"} are sent here.`}
          </p>
        </fieldset>

        <div className={m2.field}>
          <label className={m2.label} htmlFor={`${id}-sev`}>
            Minimum severity
          </label>
          <select
            id={`${id}-sev`}
            className={m2.control}
            value={draft.minSeverity}
            onChange={(e) => set("minSeverity", e.target.value as Severity)}
          >
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {severityLabel(s)}
                {s === "info" ? " (everything)" : s === "critical" ? " only" : " and critical"}
              </option>
            ))}
          </select>
        </div>

        <div className={m2.field} style={{ justifyContent: "flex-end", gap: 8 }}>
          <label className={m2.check}>
            <input type="checkbox" checked={draft.digest} onChange={(e) => set("digest", e.target.checked)} />
            Daily digest instead of one message per alert
          </label>
          <label className={m2.check}>
            <input type="checkbox" checked={draft.active} onChange={(e) => set("active", e.target.checked)} />
            Active
          </label>
        </div>

        {saveError && (
          <p className={`${m2.fieldError} ${m2.full}`} role="alert" style={{ fontSize: 14 }}>
            Couldn&apos;t save: {saveError}
          </p>
        )}
        <div className={m2.formActions}>
          <button type="submit" className={connect.primary} disabled={saving}>
            {saving ? "Saving…" : channel ? "Save changes" : "Create channel"}
          </button>
          <button type="button" className={connect.secondary} onClick={onCancel} disabled={saving}>
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}
