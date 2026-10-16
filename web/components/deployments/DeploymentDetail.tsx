"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { lastNDays, shownCost, type Deployment, type DeploymentDailyRow } from "@/lib/api";
import { formatCount, formatDay, formatMoney } from "@/lib/format";
import { useSession } from "../AuthProvider";
import { DailyCostChart } from "../DailyCostChart";
import { RateChart, type RatePoint } from "../RateChart";
import { TokensChart, type TokenPoint } from "../TokensChart";
import { daysBetween, type DailyPoint, type Series } from "../dailySeries";
import {
  EmptyNotice,
  ErrorNotice,
  Kpi,
  KpiSkeletons,
  RANGE_OPTIONS,
  Segmented,
  Skeleton,
  ui as styles,
  useLoad,
  type RangeDays,
} from "../ui";
import { TagChips } from "./TagChips";

// /deployments/{id}/daily has no currency field; v1 subscriptions bill in one currency.
const CURRENCY = "USD";
const COST_SERIES: Series[] = [{ key: "cost", label: "Cost", color: "var(--series-1)" }];

export function DeploymentDetail({ id }: { id: string }) {
  const { api } = useSession();
  const [days, setDays] = useState<RangeDays>(30);
  const range = useMemo(() => lastNDays(days), [days]);
  const [state, retry] = useLoad(
    () => api.deploymentDaily(id, range),
    `${id}|${range.start}|${range.end}`,
  );

  return (
    <main className={styles.main}>
      <div>
        <Link className={`${styles.link} ${styles.crumb}`} href="/deployments">
          ← Deployments
        </Link>
      </div>
      <div className={styles.toolbar}>
        <div style={{ minWidth: 0 }}>
          <h1 className={styles.title} style={{ overflowWrap: "anywhere" }}>
            {state.status === "ready" ? state.data.deployment.name : "Deployment"}
          </h1>
          {state.status === "ready" && (
            <p className={styles.subtitle}>
              {[state.data.deployment.model, state.data.deployment.account_name]
                .filter(Boolean)
                .join(" · ")}
            </p>
          )}
        </div>
        <Segmented label="Date range" options={RANGE_OPTIONS} value={days} onChange={setDays} />
      </div>

      {state.status === "loading" && (
        <>
          <Skeleton height={120} label="Loading deployment…" />
          <KpiSkeletons count={4} />
          <Skeleton height={360} />
        </>
      )}
      {state.status === "error" && (
        <ErrorNotice title="Couldn't load this deployment" message={state.message} onRetry={retry} />
      )}
      {state.status === "ready" && (
        <>
          <Facts d={state.data.deployment} />
          <Report rows={state.data.rows} start={range.start} end={range.end} />
        </>
      )}
    </main>
  );
}

function Facts({ d }: { d: Deployment }) {
  const resource = d.resource_id.split("/").filter(Boolean).pop() ?? d.resource_id;
  return (
    <section className={styles.panel} aria-label="Deployment details">
      <dl className={styles.dl}>
        <Fact label="Model" value={d.model} />
        <Fact label="Version" value={d.model_version} />
        <Fact label="SKU" value={d.sku && (d.capacity != null ? `${d.sku} × ${d.capacity}` : d.sku)} />
        <Fact label="Region" value={d.region} />
        <Fact label="Subscription" value={d.account_name || d.account_id} title={d.account_id} />
        <Fact label="Resource" value={resource} title={d.resource_id} />
        <div style={{ gridColumn: "1 / -1" }}>
          <dt>Tags</dt>
          <dd>
            <TagChips tags={d.tags} />
          </dd>
        </div>
      </dl>
    </section>
  );
}

function Fact({ label, value, title }: { label: string; value: string | null; title?: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd title={title}>{value || "—"}</dd>
    </div>
  );
}

function Report({ rows, start, end }: { rows: DeploymentDailyRow[]; start: string; end: string }) {
  const { costPoints, tokenPoints, ratePoints, filled } = useMemo(() => {
    const byDay = new Map(rows.map((r) => [r.day, r]));
    const filled = daysBetween(start, end).map((day) => ({ day, row: byDay.get(day) ?? null }));
    const costPoints: DailyPoint[] = filled.map(({ day, row }) => ({
      day,
      v: { cost: row ? shownCost(row) : 0 },
      est: { cost: row?.day_status === "estimated" },
    }));
    const tokenPoints: TokenPoint[] = filled.map(({ day, row }) => ({
      day,
      input: row?.input_tokens ?? 0,
      output: row?.output_tokens ?? 0,
    }));
    const ratePoints: RatePoint[] = filled.map(({ day, row }) => ({
      day,
      rate: row?.cost_per_1m_tokens != null ? Number(row.cost_per_1m_tokens) : null,
      estimated: row?.day_status === "estimated",
    }));
    return { costPoints, tokenPoints, ratePoints, filled };
  }, [rows, start, end]);

  if (rows.length === 0) {
    return (
      <EmptyNotice title="No cost or usage for this range">
        Billing and token metrics for this deployment haven&apos;t arrived yet. Try a longer
        range, or check Connections for sync status.
      </EmptyNotice>
    );
  }

  const total = rows.reduce((s, r) => s + shownCost(r), 0);
  const estimated = rows.reduce((s, r) => s + (r.day_status === "estimated" ? shownCost(r) : 0), 0);
  const input = rows.reduce((s, r) => s + r.input_tokens, 0);
  const output = rows.reduce((s, r) => s + r.output_tokens, 0);
  const tokens = input + output;
  const costWithTokens = rows.reduce(
    (s, r) => s + (r.input_tokens + r.output_tokens > 0 ? shownCost(r) : 0),
    0,
  );
  const hasRate = ratePoints.some((p) => p.rate !== null);

  return (
    <>
      <section className={styles.kpis4} aria-label="Key figures">
        <Kpi
          label="Cost for range"
          value={formatMoney(total, CURRENCY)}
          note={`${formatDay(start)} – ${formatDay(end)}`}
        />
        <Kpi
          label="Of which estimated"
          value={formatMoney(estimated, CURRENCY)}
          note="Not yet billed"
        />
        <Kpi
          label="Tokens"
          value={formatCount(tokens, true)}
          title={formatCount(tokens)}
          note={`${formatCount(input, true)} in · ${formatCount(output, true)} out`}
        />
        <Kpi
          label="$ per 1M tokens"
          value={tokens > 0 ? formatMoney((costWithTokens / tokens) * 1e6, CURRENCY) : "—"}
          note="Average over the range"
        />
      </section>

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>Daily cost</h2>
        <DailyCostChart points={costPoints} series={COST_SERIES} currency={CURRENCY} height={260} />
      </section>

      <div className={styles.charts2}>
        <section className={styles.panel}>
          <h2 className={styles.panelTitle}>Tokens per day</h2>
          {tokens > 0 ? (
            <TokensChart points={tokenPoints} />
          ) : (
            <p className={styles.muted}>No token metrics in this range.</p>
          )}
        </section>
        <section className={styles.panel}>
          <h2 className={styles.panelTitle}>$ per 1M tokens</h2>
          {hasRate ? (
            <RateChart points={ratePoints} currency={CURRENCY} />
          ) : (
            <p className={styles.muted}>Needs both cost and token data.</p>
          )}
        </section>
      </div>

      <section className={styles.panel}>
        <details className={styles.details}>
          <summary>Daily figures as a table</summary>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Day</th>
                  <th scope="col">Status</th>
                  <th scope="col" className={styles.num}>Cost</th>
                  <th scope="col" className={styles.num}>Input tokens</th>
                  <th scope="col" className={styles.num}>Output tokens</th>
                  <th scope="col" className={styles.num}>$/1M tokens</th>
                </tr>
              </thead>
              <tbody>
                {[...filled].reverse().map(({ day, row }) => (
                  <tr key={day}>
                    <th scope="row">{formatDay(day)}</th>
                    <td>{row ? row.day_status : "—"}</td>
                    <td className={styles.num}>{formatMoney(row ? shownCost(row) : 0, CURRENCY)}</td>
                    <td className={styles.num}>{formatCount(row?.input_tokens ?? 0)}</td>
                    <td className={styles.num}>{formatCount(row?.output_tokens ?? 0)}</td>
                    <td className={styles.num}>
                      {row?.cost_per_1m_tokens != null
                        ? formatMoney(Number(row.cost_per_1m_tokens), CURRENCY)
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </section>
    </>
  );
}
