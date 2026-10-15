"use client";

import { useMemo, useState } from "react";
import {
  lastNDays,
  type DailyCosts,
  type Deployment,
  type GroupBy,
  type Summary,
} from "@/lib/api";
import { formatCount, formatDay, formatMoney } from "@/lib/format";
import { useSession } from "./AuthProvider";
import { DailyCostChart } from "./DailyCostChart";
import { DiscoveryBanner } from "./DiscoveryBanner";
import { daysBetween, pivotGrouped } from "./dailySeries";
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
  type Load,
  type RangeDays,
} from "./ui";
import { YesterdayByDeployment } from "./YesterdayByDeployment";

type GroupKind = "model" | "deployment" | "account" | "tag";

const GROUP_OPTIONS = [
  { value: "model", label: "Model" },
  { value: "deployment", label: "Deployment" },
  { value: "account", label: "Subscription" },
  { value: "tag", label: "Tag" },
] as const satisfies readonly { value: GroupKind; label: string }[];

const GROUP_NOUN: Record<GroupKind, string> = {
  model: "model",
  deployment: "deployment",
  account: "subscription",
  tag: "tag",
};

export function Overview() {
  const { api } = useSession();
  const [days, setDays] = useState<RangeDays>(30);
  const range = useMemo(() => lastNDays(days), [days]);
  const rangeKey = `${range.start}|${range.end}`;

  const [summary, retrySummary] = useLoad(() => api.summary(range), rangeKey);
  // Inventory gives readable names for deployment ids and subscription ids in the chart.
  const [deployments] = useLoad(() => api.deployments(), "deployments");

  return (
    <main className={styles.main}>
      <DiscoveryBanner onTracked={retrySummary} />
      <div className={styles.toolbar}>
        <h1 className={styles.title}>Overview</h1>
        <Segmented label="Date range" options={RANGE_OPTIONS} value={days} onChange={setDays} />
      </div>

      {summary.status === "loading" && <KpiSkeletons count={4} />}
      {summary.status === "error" && (
        <ErrorNotice
          title="Couldn't load cost data"
          message={summary.message}
          onRetry={retrySummary}
        />
      )}
      {summary.status === "ready" && (
        <Kpis summary={summary.data} start={range.start} end={range.end} />
      )}

      <YesterdayByDeployment
        currency={summary.status === "ready" ? summary.data.currency || undefined : undefined}
      />

      <DailyPanel
        start={range.start}
        end={range.end}
        deployments={deployments.status === "ready" ? deployments.data : []}
      />

      {summary.status === "ready" && <ByModel summary={summary.data} />}
    </main>
  );
}

function Kpis({ summary, start, end }: { summary: Summary; start: string; end: string }) {
  const currency = summary.currency || "USD";
  // Weighted by tokens so a high-volume cheap model moves the blend as much as it should.
  let tokens = 0;
  let weighted = 0;
  for (const m of summary.by_model) {
    const t = m.input_tokens + m.output_tokens;
    if (m.cost_per_1m_tokens === null || t === 0) continue;
    tokens += t;
    weighted += Number(m.cost_per_1m_tokens) * t;
  }
  const blended = tokens > 0 ? weighted / tokens : null;
  const monthStart = `${end.slice(0, 7)}-01`;

  return (
    <section className={styles.kpis4} aria-label="Key figures">
      <Kpi
        label="Today (estimated)"
        value={formatMoney(Number(summary.today_estimated_cost), currency)}
        note="From token usage; not yet billed"
      />
      <Kpi
        label="Month to date"
        value={formatMoney(Number(summary.month_to_date_cost), currency)}
        note={`Since ${formatDay(monthStart)}`}
      />
      <Kpi
        label="Total for range"
        value={formatMoney(Number(summary.total_cost), currency)}
        note={`${formatDay(start)} – ${formatDay(end)}`}
      />
      <Kpi
        label="Blended $ per 1M tokens"
        value={blended === null ? "—" : formatMoney(blended, currency)}
        note={tokens > 0 ? `Over ${formatCount(tokens, true)} tokens` : "No token data yet"}
        title={tokens > 0 ? `${formatCount(tokens)} tokens` : undefined}
      />
    </section>
  );
}

function DailyPanel({
  start,
  end,
  deployments,
}: {
  start: string;
  end: string;
  deployments: Deployment[];
}) {
  const { api } = useSession();
  const [kind, setKind] = useState<GroupKind>("model");
  const [tagDraft, setTagDraft] = useState("");
  const [tagKey, setTagKey] = useState("");

  const groupBy: GroupBy | null =
    kind === "tag" ? (tagKey ? `tag:${tagKey}` : null) : kind;
  const [daily, retry] = useLoad<DailyCosts | null>(
    () => (groupBy ? api.dailyCosts({ start, end }, groupBy) : Promise.resolve(null)),
    `${start}|${end}|${groupBy ?? ""}`,
  );

  const labelFor = useMemo(() => {
    const names = new Map<string, string>();
    if (kind === "deployment") for (const d of deployments) names.set(d.id, d.name);
    if (kind === "account")
      for (const d of deployments) if (d.account_name) names.set(d.account_id, d.account_name);
    return (key: string) => names.get(key) ?? (key === "" ? "(untagged)" : key);
  }, [kind, deployments]);

  const applyTag = () => setTagKey(tagDraft.trim());

  return (
    <section className={styles.panel}>
      <div className={styles.panelHead}>
        <h2 className={styles.panelTitle}>Daily cost by {GROUP_NOUN[kind]}</h2>
        <div className={styles.controls}>
          <Segmented label="Group by" options={GROUP_OPTIONS} value={kind} onChange={setKind} />
          {kind === "tag" && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                applyTag();
              }}
            >
              <label className={styles.srOnly} htmlFor="tag-key">
                Tag key
              </label>
              <input
                id="tag-key"
                className={styles.input}
                placeholder="Tag key, e.g. team"
                value={tagDraft}
                onChange={(e) => setTagDraft(e.target.value)}
                onBlur={applyTag}
                autoComplete="off"
                spellCheck={false}
              />
            </form>
          )}
        </div>
      </div>
      <DailyBody
        state={daily}
        retry={retry}
        start={start}
        end={end}
        labelFor={labelFor}
        needsTag={kind === "tag" && !tagKey}
      />
    </section>
  );
}

function DailyBody({
  state,
  retry,
  start,
  end,
  labelFor,
  needsTag,
}: {
  state: Load<DailyCosts | null>;
  retry: () => void;
  start: string;
  end: string;
  labelFor: (key: string) => string;
  needsTag: boolean;
}) {
  const data = state.status === "ready" ? state.data : null;
  const pivot = useMemo(
    () => (data ? pivotGrouped(data.rows, daysBetween(start, end), labelFor) : null),
    [data, start, end, labelFor],
  );

  if (needsTag) {
    return <p className={styles.muted}>Enter a tag key (for example team or cost-center) and press Enter.</p>;
  }
  if (state.status === "loading") return <Skeleton height={330} label="Loading daily cost…" />;
  if (state.status === "error") {
    return <ErrorNotice title="Couldn't load daily cost" message={state.message} onRetry={retry} />;
  }
  if (!data || !pivot || data.rows.length === 0) {
    return (
      <EmptyNotice title="No cost data for this range">
        Once the worker has ingested billing data for your subscriptions, spend shows up here. Try
        a longer range, or run <code>act seed-demo</code> locally.
      </EmptyNotice>
    );
  }
  return <DailyCostChart points={pivot.points} series={pivot.series} currency={data.currency || "USD"} />;
}

function ByModel({ summary }: { summary: Summary }) {
  const currency = summary.currency || "USD";
  const models = [...summary.by_model].sort(
    (a, b) =>
      Number(b.billed_cost) + Number(b.estimated_cost) - (Number(a.billed_cost) + Number(a.estimated_cost)),
  );
  if (models.length === 0) return null;
  const rate = (v: string | null) => (v === null ? "—" : formatMoney(Number(v), currency));
  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>By model</h2>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col" className={styles.num}>Billed</th>
              <th scope="col" className={styles.num}>Estimated</th>
              <th scope="col" className={styles.num}>Input tokens</th>
              <th scope="col" className={styles.num}>Output tokens</th>
              <th scope="col" className={styles.num}>$/1M input</th>
              <th scope="col" className={styles.num}>$/1M output</th>
              <th scope="col" className={styles.num}>$/1M blended</th>
            </tr>
          </thead>
          <tbody>
            {models.map((m) => (
              <tr key={m.model}>
                <th scope="row">{m.model}</th>
                <td className={styles.num}>{formatMoney(Number(m.billed_cost), currency)}</td>
                <td className={styles.num}>{formatMoney(Number(m.estimated_cost), currency)}</td>
                <td className={styles.num}>{formatCount(m.input_tokens)}</td>
                <td className={styles.num}>{formatCount(m.output_tokens)}</td>
                <td className={styles.num}>{rate(m.cost_per_1m_input)}</td>
                <td className={styles.num}>{rate(m.cost_per_1m_output)}</td>
                <td className={styles.num}>{rate(m.cost_per_1m_tokens)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
