"use client";

import Link from "next/link";
import { shownCost, yesterday, type ApiClient, type DayStatus, type Deployment } from "@/lib/api";
import { formatCount, formatDay, formatMoney } from "@/lib/format";
import { useSession } from "./AuthProvider";
import { ExportButton } from "./ExportButton";
import { EmptyNotice, ErrorNotice, Skeleton, ui as styles, useLoad } from "./ui";

interface Row {
  deployment: Deployment;
  cost: number;
  tokens: number;
  per1m: number | null;
  status: DayStatus | null; // null: no cost or usage rows that day
}

interface Result {
  currency: string;
  rows: Row[];
}

// Enough parallelism to be quick without flooding the API with one request per deployment.
const CONCURRENCY = 6;

/**
 * One /deployments/{id}/daily call per deployment for a single day. It reuses the deployment
 * endpoint instead of grouping /costs/daily by deployment, so cost, tokens and the deployment
 * id (for the link) come from one place. A batch endpoint would replace this if lists grow.
 */
async function loadYesterday(api: ApiClient, day: string): Promise<Result> {
  const deployments = await api.deployments();
  const rows: Row[] = new Array(deployments.length);
  let next = 0;
  const worker = async () => {
    while (next < deployments.length) {
      const i = next++;
      const d = deployments[i];
      const res = await api.deploymentDaily(d.id, { start: day, end: day });
      const r = res.rows.find((x) => x.day === day);
      const tokens = r ? r.input_tokens + r.output_tokens : 0;
      rows[i] = {
        deployment: d,
        cost: r ? shownCost(r) : 0,
        tokens,
        per1m: r?.cost_per_1m_tokens != null ? Number(r.cost_per_1m_tokens) : null,
        status: r ? r.day_status : null,
      };
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, deployments.length) }, worker));
  rows.sort((a, b) => b.cost - a.cost || b.tokens - a.tokens || a.deployment.name.localeCompare(b.deployment.name));
  // /deployments/{id}/daily carries no currency; v1 subscriptions all bill in one, as /summary does.
  return { currency: "USD", rows };
}

/** Answers "what did each deployment cost yesterday?" (issue #23's done criterion). */
export function YesterdayByDeployment({ currency: currencyHint }: { currency?: string }) {
  const { api } = useSession();
  const day = yesterday().start;
  const [state, retry] = useLoad(() => loadYesterday(api, day), day);

  return (
    <section className={styles.panel}>
      <div className={styles.panelHead}>
        <h2 className={styles.panelTitle}>Yesterday by deployment</h2>
        <ExportButton name="costs-daily" query={{ start: day, end: day, group_by: "deployment" }} />
      </div>
      <p className={styles.panelSub} style={{ marginTop: -4 }}>{formatDay(day)} (UTC day, as billed)</p>
      {state.status === "loading" && <Skeleton height={200} label="Loading yesterday's cost…" />}
      {state.status === "error" && (
        <ErrorNotice title="Couldn't load yesterday's cost" message={state.message} onRetry={retry} />
      )}
      {state.status === "ready" && state.data.rows.length === 0 && (
        <EmptyNotice title="No deployments yet">
          Connect an Azure subscription and its AI deployments will be listed here.{" "}
          <Link className={styles.link} href="/connect">
            Connect Azure
          </Link>
        </EmptyNotice>
      )}
      {state.status === "ready" && state.data.rows.length > 0 && (
        <Table rows={state.data.rows} currency={currencyHint ?? state.data.currency} />
      )}
    </section>
  );
}

function Table({ rows, currency }: { rows: Row[]; currency: string }) {
  const totalCost = rows.reduce((s, r) => s + r.cost, 0);
  const totalTokens = rows.reduce((s, r) => s + r.tokens, 0);
  const anyEstimated = rows.some((r) => r.status === "estimated");
  // Blend only where tokens exist, so spend with no usage data doesn't inflate the rate.
  const costWithTokens = rows.reduce((s, r) => s + (r.tokens > 0 ? r.cost : 0), 0);
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Deployment · model</th>
            <th scope="col" className={styles.num}>Cost</th>
            <th scope="col" className={styles.num}>Tokens</th>
            <th scope="col" className={styles.num}>$/1M tokens</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.deployment.id}>
              <th scope="row">
                <Link className={styles.link} href={`/deployments/${encodeURIComponent(r.deployment.id)}`}>
                  {r.deployment.name}
                </Link>
                <div className={styles.cellSub}>{r.deployment.model ?? "—"}</div>
              </th>
              <td className={styles.num}>
                {r.status === "estimated" && <span className={styles.badge}>estimated</span>}
                {formatMoney(r.cost, currency)}
              </td>
              <td className={styles.num} title={formatCount(r.tokens)}>
                {formatCount(r.tokens, true)}
              </td>
              <td className={styles.num}>{r.per1m === null ? "—" : formatMoney(r.per1m, currency)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row">Total</th>
            <td className={styles.num}>{formatMoney(totalCost, currency)}</td>
            <td className={styles.num} title={formatCount(totalTokens)}>
              {formatCount(totalTokens, true)}
            </td>
            <td className={styles.num}>
              {totalTokens > 0 ? formatMoney((costWithTokens / totalTokens) * 1e6, currency) : "—"}
            </td>
          </tr>
        </tfoot>
      </table>
      {anyEstimated && (
        <p className={styles.muted} style={{ fontSize: 13, marginTop: 8 }}>
          “estimated” rows are not billed yet; Azure billing lags 24–72 hours.
        </p>
      )}
    </div>
  );
}
