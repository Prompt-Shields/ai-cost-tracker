"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, lastNDays, type DailyCosts, type Summary } from "@/lib/api";
import { formatCount, formatMoney } from "@/lib/format";
import { OTHER, daysBetween, pivotDaily } from "@/lib/series";
import { useSession } from "./AuthProvider";
import { DailyCostChart } from "./DailyCostChart";
import styles from "./Overview.module.css";

const RANGES = [7, 30, 90] as const;
type RangeDays = (typeof RANGES)[number];

type State =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; summary: Summary; daily: DailyCosts };

export function Overview() {
  const { api } = useSession();
  const [days, setDays] = useState<RangeDays>(30);
  const [state, setState] = useState<State>({ status: "loading" });
  const [reloadKey, setReloadKey] = useState(0);
  const range = useMemo(() => lastNDays(days), [days]);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    Promise.all([api.summary(range), api.dailyCosts(range)])
      .then(([summary, daily]) => !cancelled && setState({ status: "ready", summary, daily }))
      .catch((err: unknown) => {
        if (cancelled) return;
        const message =
          err instanceof ApiError && (err.status === 401 || err.status === 403)
            ? "You don't have access to cost data. Ask an admin to add you to an allowed group."
            : err instanceof Error
              ? err.message
              : "Something went wrong.";
        setState({ status: "error", message });
      });
    return () => {
      cancelled = true;
    };
  }, [api, range, reloadKey]);

  const retry = useCallback(() => setReloadKey((k) => k + 1), []);

  return (
    <main className={styles.main}>
      <div className={styles.toolbar}>
        <h1 className={styles.title}>Overview</h1>
        <div className={styles.segmented} role="group" aria-label="Date range">
          {RANGES.map((d) => (
            <button
              key={d}
              type="button"
              aria-pressed={d === days}
              className={d === days ? styles.segActive : styles.seg}
              onClick={() => setDays(d)}
            >
              {d} days
            </button>
          ))}
        </div>
      </div>

      {state.status === "loading" && <Loading />}
      {state.status === "error" && (
        <div className={styles.notice} role="alert">
          <p className={styles.noticeTitle}>Couldn&apos;t load cost data</p>
          <p className={styles.noticeBody}>{state.message}</p>
          <button type="button" className={styles.retry} onClick={retry}>
            Try again
          </button>
        </div>
      )}
      {state.status === "ready" && (
        <Report summary={state.summary} daily={state.daily} start={range.start} end={range.end} />
      )}
    </main>
  );
}

function Report({
  summary,
  daily,
  start,
  end,
}: {
  summary: Summary;
  daily: DailyCosts;
  start: string;
  end: string;
}) {
  const currency = summary.currency || daily.currency || "USD";
  const { series, points } = useMemo(
    () => pivotDaily(daily.rows, daysBetween(start, end)),
    [daily.rows, start, end],
  );

  if (summary.by_model.length === 0 && daily.rows.length === 0) {
    return (
      <div className={styles.notice}>
        <p className={styles.noticeTitle}>No cost data for this range</p>
        <p className={styles.noticeBody}>
          Once the worker has ingested billing data for your subscriptions, spend will show up
          here. Try a longer range, or run <code>act seed-demo</code> locally.
        </p>
      </div>
    );
  }

  const totalCost = Number(summary.total_cost);
  const totalTokens = summary.by_model.reduce((s, m) => s + m.input_tokens + m.output_tokens, 0);
  // Blend over models that report tokens, so unmapped/no-token spend doesn't inflate the rate.
  const withTokens = summary.by_model.filter((m) => m.input_tokens + m.output_tokens > 0);
  const costWithTokens = withTokens.reduce((s, m) => s + Number(m.billed_cost), 0);
  const blended = totalTokens > 0 ? (costWithTokens / totalTokens) * 1e6 : null;

  const colorFor = new Map(series.map((s) => [s.key, s.color]));
  const models = [...summary.by_model].sort((a, b) => Number(b.billed_cost) - Number(a.billed_cost));

  return (
    <>
      <section className={styles.kpis} aria-label="Key figures">
        <Kpi label="Total cost" value={formatMoney(totalCost, currency)} />
        <Kpi label="Total tokens" value={formatCount(totalTokens, true)} title={formatCount(totalTokens)} />
        <Kpi
          label="Blended cost per 1M tokens"
          value={blended === null ? "—" : formatMoney(blended, currency)}
        />
      </section>

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>Daily cost by model</h2>
        {daily.rows.length === 0 ? (
          <p className={styles.muted}>No daily cost rows in this range.</p>
        ) : (
          <DailyCostChart points={points} series={series} currency={currency} />
        )}
      </section>

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>By model</h2>
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th scope="col">Model</th>
                <th scope="col" className={styles.num}>Cost</th>
                <th scope="col" className={styles.num}>Input tokens</th>
                <th scope="col" className={styles.num}>Output tokens</th>
                <th scope="col" className={styles.num}>Per 1M tokens</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <tr key={m.model}>
                  <th scope="row">
                    <span
                      className={styles.swatch}
                      style={{ background: colorFor.get(m.model) ?? colorFor.get(OTHER) ?? "transparent" }}
                      aria-hidden
                    />
                    {m.model}
                  </th>
                  <td className={styles.num}>{formatMoney(Number(m.billed_cost), currency)}</td>
                  <td className={styles.num}>{formatCount(m.input_tokens)}</td>
                  <td className={styles.num}>{formatCount(m.output_tokens)}</td>
                  <td className={styles.num}>
                    {m.cost_per_1m_tokens === null
                      ? "—"
                      : formatMoney(Number(m.cost_per_1m_tokens), currency)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}

function Kpi({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className={styles.kpi}>
      <div className={styles.kpiLabel}>{label}</div>
      <div className={styles.kpiValue} title={title}>
        {value}
      </div>
    </div>
  );
}

function Loading() {
  return (
    <div aria-busy="true" aria-live="polite">
      <span className={styles.srOnly}>Loading cost data…</span>
      <div className={styles.kpis}>
        {[0, 1, 2].map((i) => (
          <div key={i} className={`${styles.kpi} ${styles.skeleton}`} style={{ height: 84 }} />
        ))}
      </div>
      <div className={`${styles.panel} ${styles.skeleton}`} style={{ height: 360 }} />
    </div>
  );
}
