"use client";

import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { lastNDays } from "@/lib/api";
import {
  AllocApi,
  UNALLOCATED,
  type AllocationLevel,
  type CostCentreDailyRow,
} from "@/lib/api-m2-alloc";
import { formatMoney } from "@/lib/format";
import { useSession } from "../AuthProvider";
import { ChartLegend, TooltipBox, TooltipRow, chartStyles, xAxisProps, yAxisBase } from "../chartParts";
import { daysBetween } from "../dailySeries";
import {
  EmptyNotice,
  ErrorNotice,
  Kpi,
  KpiSkeletons,
  RANGE_OPTIONS,
  Segmented,
  Skeleton,
  ui,
  useLoad,
  type RangeDays,
} from "../ui";
import m2 from "./m2.module.css";

const LEVEL_OPTIONS = [
  { value: "cost_centre", label: "Cost centre" },
  { value: "team", label: "Team" },
  { value: "app", label: "App" },
] as const satisfies readonly { value: AllocationLevel; label: string }[];

const LEVEL_NOUN: Record<AllocationLevel, string> = {
  cost_centre: "Cost centre",
  team: "Team",
  app: "App",
};

const APPROX_NOTE = "Includes split or shared-resource estimates";
// Seven categorical slots; overflow folds into "Other" (hatched neutral), never a new hue.
const SLOTS = 7;
const OTHER = "__other__";
const OTHER_PATTERN = "cc-other-hatch";
const OTHER_SWATCH =
  "repeating-linear-gradient(45deg, var(--series-other) 0 2px, transparent 2px 4px)";
const APPROX_OPACITY = "var(--estimated-opacity)";

interface Series {
  key: string;
  label: string;
  color: string; // CSS colour for legend / tooltip
  fill: string; // SVG fill for the bar
}

interface Point {
  day: string;
  v: Record<string, number>;
  approx: Record<string, boolean>;
}

interface Total {
  key: string;
  label: string;
  color: string;
  cost: number;
  approximate: boolean;
}

/** Ranks keys by cost, keeps "unallocated" last in a neutral colour, folds overflow to Other. */
function pivot(rows: CostCentreDailyRow[], days: string[]) {
  const totals = new Map<string, { cost: number; approx: boolean }>();
  for (const r of rows) {
    const t = totals.get(r.key) ?? { cost: 0, approx: false };
    t.cost += Number(r.cost);
    t.approx ||= r.approximate;
    totals.set(r.key, t);
  }
  const ranked = [...totals.entries()]
    .filter(([k]) => k !== UNALLOCATED)
    .sort((a, b) => b[1].cost - a[1].cost)
    .map(([k]) => k);
  const named = ranked.length > SLOTS ? ranked.slice(0, SLOTS - 1) : ranked;
  const namedSet = new Set(named);

  const series: Series[] = named.map((key, i) => ({
    key,
    label: key,
    color: `var(--series-${i + 1})`,
    fill: `var(--series-${i + 1})`,
  }));
  if (ranked.length > named.length) {
    series.push({ key: OTHER, label: "Other", color: OTHER_SWATCH, fill: `url(#${OTHER_PATTERN})` });
  }
  if (totals.has(UNALLOCATED)) {
    series.push({
      key: UNALLOCATED,
      label: "Unallocated",
      color: "var(--series-other)",
      fill: "var(--series-other)",
    });
  }

  const byDay = new Map<string, Point>(days.map((d) => [d, { day: d, v: {}, approx: {} }]));
  for (const r of rows) {
    const p = byDay.get(r.day);
    if (!p) continue;
    const key = r.key === UNALLOCATED || namedSet.has(r.key) ? r.key : OTHER;
    p.v[key] = (p.v[key] ?? 0) + Number(r.cost);
    if (r.approximate) p.approx[key] = true;
  }

  const colorOf = new Map(series.map((s) => [s.key, s.color]));
  const table: Total[] = [...totals.entries()]
    .map(([key, t]) => ({
      key,
      label: key === UNALLOCATED ? "Unallocated" : key,
      color: colorOf.get(key) ?? OTHER_SWATCH,
      cost: t.cost,
      approximate: t.approx,
    }))
    .sort((a, b) =>
      a.key === UNALLOCATED ? 1 : b.key === UNALLOCATED ? -1 : b.cost - a.cost,
    );
  return { series, points: [...byDay.values()], table };
}

export function CostCentres() {
  const { api } = useSession();
  const alloc = useMemo(() => new AllocApi(api), [api]);
  const [days, setDays] = useState<RangeDays>(30);
  const [level, setLevel] = useState<AllocationLevel>("cost_centre");
  const range = useMemo(() => lastNDays(days), [days]);
  const [state, retry] = useLoad(
    () => alloc.costCentresDaily(range, level),
    `${range.start}|${range.end}|${level}`,
  );

  const view = useMemo(() => {
    if (state.status !== "ready") return null;
    return pivot(state.data.rows, daysBetween(range.start, range.end));
  }, [state, range]);
  const currency = state.status === "ready" ? state.data.currency || "USD" : "USD";
  const total = view ? view.table.reduce((s, t) => s + t.cost, 0) : 0;
  const unallocated = view?.table.find((t) => t.key === UNALLOCATED)?.cost ?? 0;
  const approxCost = view ? view.table.filter((t) => t.approximate).reduce((s, t) => s + t.cost, 0) : 0;

  return (
    <main className={`${ui.main} ${m2.root}`}>
      <div className={ui.toolbar}>
        <h1 className={ui.title}>Cost centres</h1>
        <div className={ui.controls}>
          <Segmented label="Allocate by" options={LEVEL_OPTIONS} value={level} onChange={setLevel} />
          <Segmented label="Date range" options={RANGE_OPTIONS} value={days} onChange={setDays} />
        </div>
      </div>

      {state.status === "loading" && (
        <>
          <KpiSkeletons count={3} />
          <Skeleton height={360} label="Loading allocated cost…" />
        </>
      )}
      {state.status === "error" && (
        <ErrorNotice title="Couldn't load allocated cost" message={state.message} onRetry={retry} />
      )}
      {view && view.table.length === 0 && (
        <EmptyNotice title="No cost in this range">
          Allocated cost appears once billing data has been ingested for your subscriptions.
        </EmptyNotice>
      )}
      {view && view.table.length > 0 && (
        <>
          <section className={ui.kpis} aria-label="Key figures">
            <Kpi label="Total for range" value={formatMoney(total, currency)} />
            <Kpi
              label="Unallocated"
              value={formatMoney(unallocated, currency)}
              note={total > 0 ? `${((unallocated / total) * 100).toFixed(1)}% of spend matched no rule` : undefined}
            />
            <Kpi
              label="Approximate"
              value={formatMoney(approxCost, currency)}
              note="In rows that include split or shared-resource estimates"
            />
          </section>

          <section className={ui.panel}>
            <h2 className={ui.panelTitle}>Daily cost by {LEVEL_NOUN[level].toLowerCase()}</h2>
            <CostCentreChart points={view.points} series={view.series} currency={currency} />
          </section>

          <section className={ui.panel}>
            <h2 className={ui.panelTitle}>Totals</h2>
            <div className={ui.tableWrap}>
              <table className={ui.table}>
                <thead>
                  <tr>
                    <th scope="col">{LEVEL_NOUN[level]}</th>
                    <th scope="col" className={ui.num}>
                      Cost
                    </th>
                    <th scope="col" className={ui.num}>
                      Share
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {view.table.map((t) => (
                    <tr key={t.key}>
                      <th scope="row">
                        <span className={ui.swatch} style={{ background: t.color }} aria-hidden />
                        {t.label}
                        {t.approximate && (
                          <span className={ui.badge} title={APPROX_NOTE}>
                            approx.
                          </span>
                        )}
                      </th>
                      <td className={ui.num}>{formatMoney(t.cost, currency)}</td>
                      <td className={ui.num}>
                        {total > 0 ? `${((t.cost / total) * 100).toFixed(1)}%` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <th scope="row">Total</th>
                    <td className={ui.num}>{formatMoney(total, currency)}</td>
                    <td className={ui.num}>100%</td>
                  </tr>
                </tfoot>
              </table>
            </div>
            {view.table.some((t) => t.approximate) && (
              <p className={ui.muted} style={{ fontSize: 13, marginTop: 8 }}>
                approx.: {APPROX_NOTE.toLowerCase()}.
              </p>
            )}
          </section>
        </>
      )}
    </main>
  );
}

function CostCentreChart({
  points,
  series,
  currency,
}: {
  points: Point[];
  series: Series[];
  currency: string;
}) {
  const anyApprox = points.some((p) => Object.values(p.approx).some(Boolean));
  return (
    <div className={chartStyles.wrap}>
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <defs>
            <pattern
              id={OTHER_PATTERN}
              width="4"
              height="4"
              patternUnits="userSpaceOnUse"
              patternTransform="rotate(45)"
            >
              <rect width="4" height="4" fill="var(--surface-1)" />
              <rect width="2" height="4" fill="var(--series-other)" />
            </pattern>
          </defs>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis {...xAxisProps} />
          <YAxis {...yAxisBase} tickFormatter={(v: number) => formatMoney(v, currency, true)} />
          <Tooltip
            cursor={{ fill: "var(--wash)" }}
            content={({ active, payload }) => {
              const point = payload?.[0]?.payload as Point | undefined;
              if (!active || !point) return null;
              const items = series
                .map((s) => ({ ...s, value: point.v[s.key] ?? 0, approx: !!point.approx[s.key] }))
                .filter((s) => s.value > 0)
                .reverse();
              const sum = items.reduce((a, s) => a + s.value, 0);
              return (
                <TooltipBox day={point.day}>
                  {items.map((s) => (
                    <TooltipRow
                      key={s.key}
                      color={s.color}
                      label={s.approx ? `${s.label} ≈` : s.label}
                      value={formatMoney(s.value, currency)}
                    />
                  ))}
                  {items.length !== 1 && (
                    <TooltipRow label="Total" value={formatMoney(sum, currency)} total />
                  )}
                  {items.some((s) => s.approx) && (
                    <div className={chartStyles.tooltipNote}>≈ {APPROX_NOTE.toLowerCase()}</div>
                  )}
                </TooltipBox>
              );
            }}
          />
          {series.map((s, i) => (
            <Bar
              key={s.key}
              dataKey={(p: Point) => p.v[s.key] ?? 0}
              name={s.label}
              stackId="cost"
              fill={s.fill}
              maxBarSize={24}
              stroke="var(--surface-1)"
              strokeWidth={1}
              radius={i === series.length - 1 ? [4, 4, 0, 0] : 0}
              isAnimationActive={false}
            >
              {points.map((p) => (
                <Cell key={p.day} style={p.approx[s.key] ? { fillOpacity: APPROX_OPACITY } : undefined} />
              ))}
            </Bar>
          ))}
        </BarChart>
      </ResponsiveContainer>
      <ChartLegend entries={series.map((s) => ({ label: s.label, color: s.color }))} />
      {anyApprox && (
        <ul className={chartStyles.legend} style={{ marginTop: 4 }}>
          <li className={chartStyles.legendNote}>
            <span
              className={`${chartStyles.swatch} ${chartStyles.swatchEstimated}`}
              style={{ background: "var(--text-secondary)" }}
              aria-hidden
            />
            Lighter segments: {APPROX_NOTE.toLowerCase()}
          </li>
        </ul>
      )}
    </div>
  );
}
