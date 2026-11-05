"use client";

import { useMemo, useState } from "react";
import {
  lastNDays,
  type Cloud,
  type CloudInfo,
  type DailyCosts,
  type Deployment,
  type GroupBy,
  type Summary,
} from "@/lib/api";
import { currencySymbol, formatCount, formatDay, formatMoney } from "@/lib/format";
import { useSession } from "./AuthProvider";
import { cloudLabel } from "./CloudBadge";
import {
  CloudFilter,
  CurrencyNote,
  accountNoun,
  cloudColor,
  cloudFilterStyles,
  cloudQuery,
  useClouds,
  type CloudChoice,
} from "./CloudFilter";
import { DailyCostChart } from "./DailyCostChart";
import { DiscoveryBanner } from "./DiscoveryBanner";
import { ExportButton } from "./ExportButton";
import { daysBetween, pivotGrouped } from "./dailySeries";
import { SpendByCloud } from "./SpendByCloud";
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

type GroupKind = "model" | "deployment" | "account" | "cloud" | "tag";

function capitalise(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Prefix for money that went through an approximate exchange rate. */
function approxMoney(value: number, currency: string, approximate: boolean): string {
  return `${approximate ? "≈ " : ""}${formatMoney(value, currency)}`;
}

/**
 * The cloud a view is about when there is exactly one, so account wording fits ("project" on
 * Google Cloud, "account" on AWS). Without /clouds (older API) it stays Azure, as before
 * multi-cloud.
 */
function singleCloud(choice: CloudChoice, clouds: CloudInfo[]): string | null {
  if (choice !== "all") return choice;
  if (clouds.length === 0) return "azure";
  if (clouds.length === 1) return clouds[0].cloud;
  return null;
}

export function Overview() {
  const { api } = useSession();
  const [days, setDays] = useState<RangeDays>(30);
  const range = useMemo(() => lastNDays(days), [days]);
  const rangeKey = `${range.start}|${range.end}`;

  const { clouds } = useClouds();
  const multiCloud = clouds.length > 1;
  const [choice, setChoice] = useState<CloudChoice>("all");
  // Drop a filter whose cloud is no longer listed rather than showing an empty page.
  const cloud: CloudChoice =
    multiCloud && (choice === "all" || clouds.some((c) => c.cloud === choice)) ? choice : "all";
  const filters = cloudQuery(cloud);

  const [summary, retrySummary] = useLoad(
    () => api.summary(range, filters),
    `${rangeKey}|${cloud}`,
  );
  // Inventory gives readable names for deployment ids and account ids in the chart.
  const [deployments] = useLoad(() => api.deployments(), "deployments");
  const summaryData = summary.status === "ready" ? summary.data : null;

  return (
    <main className={styles.main}>
      <DiscoveryBanner onTracked={retrySummary} />
      <div className={styles.toolbar}>
        <h1 className={styles.title}>Overview</h1>
        <div className={`${styles.controls} ${cloudFilterStyles.shrink}`}>
          <CloudFilter clouds={clouds} value={cloud} onChange={setChoice} />
          <div className={cloudFilterStyles.scrollX}>
            <Segmented label="Date range" options={RANGE_OPTIONS} value={days} onChange={setDays} />
          </div>
        </div>
      </div>
      {summaryData && (
        <CurrencyNote
          currency={summaryData.currency || "USD"}
          multiCloud={multiCloud && cloud === "all"}
          approximate={summaryData.fx_approximate === true}
        />
      )}

      {summary.status === "loading" && <KpiSkeletons count={4} />}
      {summary.status === "error" && (
        <ErrorNotice
          title="Couldn't load cost data"
          message={summary.message}
          onRetry={retrySummary}
        />
      )}
      {summaryData && <Kpis summary={summaryData} start={range.start} end={range.end} />}

      {multiCloud && cloud === "all" && summary.status === "loading" && (
        <Skeleton height={200} label="Loading spend by cloud…" />
      )}
      {multiCloud && cloud === "all" && summaryData && (
        <SpendByCloud
          summary={summaryData}
          clouds={clouds}
          start={range.start}
          end={range.end}
          onSelect={(c: Cloud) => setChoice(c)}
        />
      )}

      <YesterdayByDeployment
        currency={summaryData ? summaryData.currency || undefined : undefined}
        cloud={cloud}
        showCloud={multiCloud && cloud === "all"}
        cloudName={singleCloud(cloud, clouds)}
      />

      <DailyPanel
        start={range.start}
        end={range.end}
        deployments={deployments.status === "ready" ? deployments.data : []}
        cloud={cloud}
        clouds={clouds}
      />

      {summaryData && (
        <ByModel summary={summaryData} start={range.start} end={range.end} cloud={cloud} />
      )}
    </main>
  );
}

function Kpis({ summary, start, end }: { summary: Summary; start: string; end: string }) {
  const currency = summary.currency || "USD";
  const approx = summary.fx_approximate === true;
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
        value={approxMoney(Number(summary.today_estimated_cost), currency, approx)}
        note="From token usage; not yet billed"
      />
      <Kpi
        label="Month to date"
        value={approxMoney(Number(summary.month_to_date_cost), currency, approx)}
        note={`Since ${formatDay(monthStart)}`}
      />
      <Kpi
        label="Total for range"
        value={approxMoney(Number(summary.total_cost), currency, approx)}
        note={`${formatDay(start)} – ${formatDay(end)}`}
      />
      <Kpi
        label={`Blended ${currencySymbol(currency)} per 1M tokens`}
        value={blended === null ? "—" : approxMoney(blended, currency, approx)}
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
  cloud,
  clouds,
}: {
  start: string;
  end: string;
  deployments: Deployment[];
  cloud: CloudChoice;
  clouds: CloudInfo[];
}) {
  const { api } = useSession();
  // null until the user picks: then "cloud" when clouds can be compared, else "model".
  const [picked, setPicked] = useState<GroupKind | null>(null);
  const [tagDraft, setTagDraft] = useState("");
  const [tagKey, setTagKey] = useState("");

  const canGroupByCloud = clouds.length > 1 && cloud === "all";
  const fallback: GroupKind = canGroupByCloud ? "cloud" : "model";
  const kind: GroupKind = picked === null || (picked === "cloud" && !canGroupByCloud) ? fallback : picked;
  const one = singleCloud(cloud, clouds);
  const accountWord = one ? accountNoun(one) : "account";

  const options = useMemo(() => {
    const opts: { value: GroupKind; label: string }[] = [];
    if (canGroupByCloud) opts.push({ value: "cloud", label: "Cloud" });
    opts.push(
      { value: "model", label: "Model" },
      { value: "deployment", label: "Deployment" },
      { value: "account", label: capitalise(accountWord) },
      { value: "tag", label: "Tag" },
    );
    return opts;
  }, [canGroupByCloud, accountWord]);

  const noun: Record<GroupKind, string> = {
    model: "model",
    deployment: "deployment",
    account: accountWord,
    cloud: "cloud",
    tag: "tag",
  };

  const groupBy: GroupBy | null = kind === "tag" ? (tagKey ? `tag:${tagKey}` : null) : kind;
  const filters = cloudQuery(cloud);
  const [daily, retry] = useLoad<DailyCosts | null>(
    () => (groupBy ? api.dailyCosts({ start, end }, groupBy, filters) : Promise.resolve(null)),
    `${start}|${end}|${groupBy ?? ""}|${cloud}`,
  );

  const labelFor = useMemo(() => {
    const names = new Map<string, string>();
    if (kind === "deployment") for (const d of deployments) names.set(d.id, d.name);
    if (kind === "account")
      for (const d of deployments) if (d.account_name) names.set(d.account_id, d.account_name);
    if (kind === "cloud") return (key: string) => cloudLabel(key);
    return (key: string) => names.get(key) ?? (key === "" ? "(untagged)" : key);
  }, [kind, deployments]);
  const colorFor = kind === "cloud" ? cloudColor : undefined;

  const applyTag = () => setTagKey(tagDraft.trim());

  return (
    <section className={styles.panel}>
      <div className={styles.panelHead}>
        <h2 className={styles.panelTitle}>Daily cost by {noun[kind]}</h2>
        <div className={`${styles.controls} ${cloudFilterStyles.shrink}`}>
          <div className={cloudFilterStyles.scrollX}>
            <Segmented label="Group by" options={options} value={kind} onChange={setPicked} />
          </div>
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
          {groupBy && (
            <ExportButton
              name="costs-daily"
              query={{ start, end, group_by: groupBy, ...filters }}
            />
          )}
        </div>
      </div>
      <DailyBody
        state={daily}
        retry={retry}
        start={start}
        end={end}
        labelFor={labelFor}
        colorFor={colorFor}
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
  colorFor,
  needsTag,
}: {
  state: Load<DailyCosts | null>;
  retry: () => void;
  start: string;
  end: string;
  labelFor: (key: string) => string;
  colorFor?: (key: string) => string | undefined;
  needsTag: boolean;
}) {
  const data = state.status === "ready" ? state.data : null;
  const pivot = useMemo(
    () => (data ? pivotGrouped(data.rows, daysBetween(start, end), labelFor, colorFor) : null),
    [data, start, end, labelFor, colorFor],
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
        Once the worker has ingested billing data for your cloud accounts, spend shows up here.
        Try a longer range, or run <code>act seed-demo</code> locally.
      </EmptyNotice>
    );
  }
  return (
    <>
      <DailyCostChart points={pivot.points} series={pivot.series} currency={data.currency || "USD"} />
      {data.fx_approximate === true && (
        <p className={styles.cellSub} style={{ marginTop: 8 }}>
          ≈ Some days use an approximate exchange rate.
        </p>
      )}
    </>
  );
}

function ByModel({
  summary,
  start,
  end,
  cloud,
}: {
  summary: Summary;
  start: string;
  end: string;
  cloud: CloudChoice;
}) {
  const currency = summary.currency || "USD";
  const sym = currencySymbol(currency);
  const models = [...summary.by_model].sort(
    (a, b) =>
      Number(b.billed_cost) + Number(b.estimated_cost) - (Number(a.billed_cost) + Number(a.estimated_cost)),
  );
  if (models.length === 0) return null;
  const rate = (v: string | null) => (v === null ? "—" : formatMoney(Number(v), currency));
  return (
    <section className={styles.panel}>
      <div className={styles.panelHead}>
        <h2 className={styles.panelTitle}>By model</h2>
        <ExportButton name="summary" query={{ start, end, ...cloudQuery(cloud) }} />
      </div>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col" className={styles.num}>Billed</th>
              <th scope="col" className={styles.num}>Estimated</th>
              <th scope="col" className={styles.num}>Input tokens</th>
              <th scope="col" className={styles.num}>Output tokens</th>
              <th scope="col" className={styles.num}>{sym}/1M input</th>
              <th scope="col" className={styles.num}>{sym}/1M output</th>
              <th scope="col" className={styles.num}>{sym}/1M blended</th>
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
