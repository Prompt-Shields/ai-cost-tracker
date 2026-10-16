"use client";

import { useMemo } from "react";
import { AlertsApi, type Severity } from "@/lib/api-m2-alerts";
import { useSession } from "../AuthProvider";
import styles from "./m2.module.css";

export { styles as m2 };

/** Budgets / notifications / alerts client sharing the session's API token. */
export function useAlertsApi(): AlertsApi {
  const { api } = useSession();
  return useMemo(() => new AlertsApi(api.getToken), [api]);
}

export type Tone = "ok" | "info" | "warning" | "critical" | "off";

/** Colour lives in the dot; the label stays at full contrast in both themes. */
export function Pill({ tone, label, title }: { tone: Tone; label: string; title?: string }) {
  return (
    <span className={`${styles.pill} ${styles[tone]}`} title={title}>
      <span className={styles.dot} aria-hidden />
      {label}
    </span>
  );
}

const SEVERITY_LABEL: Record<Severity, string> = {
  info: "Info",
  warning: "Warning",
  critical: "Critical",
};

export function SeverityPill({ severity }: { severity: Severity }) {
  const tone: Tone = severity === "critical" ? "critical" : severity === "info" ? "info" : "warning";
  return <Pill tone={tone} label={SEVERITY_LABEL[severity] ?? severity} />;
}

export function severityLabel(s: Severity): string {
  return SEVERITY_LABEL[s] ?? s;
}

/** Local date and time, short; the full ISO stays in the title / dateTime attribute. */
export function formatWhen(iso: string | null): string {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function When({ iso, empty = "–" }: { iso: string | null; empty?: string }) {
  if (!iso) return <span className={styles.dim}>{empty}</span>;
  return (
    <time dateTime={iso} title={new Date(iso).toString()}>
      {formatWhen(iso)}
    </time>
  );
}
