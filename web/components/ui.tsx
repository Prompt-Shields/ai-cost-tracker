"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api";
import styles from "./ui.module.css";

export { styles as ui };

export type Load<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: T };

/** Turns an API failure into a sentence a teammate can act on. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
    return "You don't have access to cost data. Ask an admin to add you to an allowed group.";
  }
  if (err instanceof ApiError && err.status === 404) return "Not found. It may have been deleted.";
  return err instanceof Error ? err.message : "Something went wrong.";
}

/**
 * Runs `fn` whenever `key` changes and ignores stale responses, so switching ranges quickly
 * never shows an older result. `retry` re-runs with the same key.
 */
export function useLoad<T>(fn: () => Promise<T>, key: string): [Load<T>, () => void] {
  const [state, setState] = useState<Load<T>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fn()
      .then((data) => !cancelled && setState({ status: "ready", data }))
      .catch((err: unknown) => !cancelled && setState({ status: "error", message: errorMessage(err) }));
    return () => {
      cancelled = true;
    };
    // `fn` is recreated each render; `key` captures everything it depends on.
  }, [key, attempt]);
  const retry = useCallback(() => setAttempt((a) => a + 1), []);
  return [state, retry];
}

export function Segmented<T extends string | number>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className={styles.segmented} role="group" aria-label={label}>
      {options.map((o) => (
        <button
          key={String(o.value)}
          type="button"
          aria-pressed={o.value === value}
          className={o.value === value ? styles.segActive : styles.seg}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export const RANGE_OPTIONS = [
  { value: 7, label: "7 days" },
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
] as const;
export type RangeDays = (typeof RANGE_OPTIONS)[number]["value"];

export function Kpi({
  label,
  value,
  note,
  title,
}: {
  label: string;
  value: string;
  note?: string;
  title?: string;
}) {
  return (
    <div className={styles.kpi}>
      <div className={styles.kpiLabel}>{label}</div>
      <div className={styles.kpiValue} title={title}>
        {value}
      </div>
      {note && <div className={styles.kpiNote}>{note}</div>}
    </div>
  );
}

export function ErrorNotice({
  title,
  message,
  onRetry,
}: {
  title: string;
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className={styles.notice} role="alert">
      <p className={styles.noticeTitle}>{title}</p>
      <p className={styles.noticeBody}>{message}</p>
      {onRetry && (
        <button type="button" className={styles.retry} onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyNotice({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className={styles.notice}>
      <p className={styles.noticeTitle}>{title}</p>
      {children && <p className={styles.noticeBody}>{children}</p>}
    </div>
  );
}

/** Placeholder block with the same footprint as what it stands in for, so nothing jumps. */
export function Skeleton({ height, label }: { height: number; label?: string }) {
  return (
    <div
      className={`${styles.panel} ${styles.skeleton}`}
      style={{ height }}
      aria-busy="true"
      aria-live="polite"
    >
      {label && <span className={styles.srOnly}>{label}</span>}
    </div>
  );
}

export function KpiSkeletons({ count }: { count: number }) {
  return (
    <div className={count === 4 ? styles.kpis4 : styles.kpis} aria-hidden>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className={`${styles.kpi} ${styles.skeleton}`} style={{ height: 96 }} />
      ))}
    </div>
  );
}
