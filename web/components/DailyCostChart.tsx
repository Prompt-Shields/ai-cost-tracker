"use client";

import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatMoney } from "@/lib/format";
import {
  ChartLegend,
  ESTIMATED_OPACITY,
  TooltipBox,
  TooltipRow,
  chartStyles as styles,
  xAxisProps,
  yAxisBase,
} from "./chartParts";
import type { DailyPoint, Series } from "./dailySeries";

interface Props {
  points: DailyPoint[];
  series: Series[];
  currency: string;
  height?: number;
  /** Hide the series legend when the panel title already names the only series. */
  showSeriesLegend?: boolean;
}

/** Stacked daily cost; estimated segments are drawn lighter so billed and estimated never blur. */
export function DailyCostChart({
  points,
  series,
  currency,
  height = 300,
  showSeriesLegend = series.length > 1,
}: Props) {
  const anyEstimated = points.some((p) => Object.values(p.est).some(Boolean));
  return (
    <div className={styles.wrap}>
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis {...xAxisProps} />
          <YAxis {...yAxisBase} tickFormatter={(v: number) => formatMoney(v, currency, true)} />
          <Tooltip
            cursor={{ fill: "var(--wash)" }}
            content={({ active, payload }) => {
              const point = payload?.[0]?.payload as DailyPoint | undefined;
              if (!active || !point) return null;
              return <CostTooltip point={point} series={series} currency={currency} />;
            }}
          />
          {series.map((s, i) => (
            <Bar
              key={s.key}
              dataKey={(p: DailyPoint) => p.v[s.key] ?? 0}
              name={s.label}
              stackId="cost"
              fill={s.color}
              maxBarSize={24}
              // Surface-coloured edge keeps a visible gap between stacked segments.
              stroke="var(--surface-1)"
              strokeWidth={1}
              radius={i === series.length - 1 ? [4, 4, 0, 0] : 0}
              isAnimationActive={false}
            >
              {points.map((p) => (
                <Cell key={p.day} style={p.est[s.key] ? { fillOpacity: ESTIMATED_OPACITY } : undefined} />
              ))}
            </Bar>
          ))}
        </BarChart>
      </ResponsiveContainer>
      <ChartLegend
        entries={showSeriesLegend ? series.map((s) => ({ label: s.label, color: s.color })) : []}
        estimatedNote={anyEstimated}
      />
    </div>
  );
}

function CostTooltip({
  point,
  series,
  currency,
}: {
  point: DailyPoint;
  series: Series[];
  currency: string;
}) {
  const items = series
    .map((s) => ({ ...s, value: point.v[s.key] ?? 0, estimated: Boolean(point.est[s.key]) }))
    .filter((s) => s.value > 0)
    .reverse(); // Match the visual stack order (top segment first).
  const total = items.reduce((sum, s) => sum + s.value, 0);
  const allEstimated = items.length > 0 && items.every((s) => s.estimated);
  return (
    <TooltipBox day={point.day} estimated={allEstimated}>
      {items.map((s) => (
        <TooltipRow
          key={s.key}
          color={s.color}
          label={s.label}
          value={formatMoney(s.value, currency)}
          estimated={s.estimated && !allEstimated}
        />
      ))}
      {items.length !== 1 && (
        <TooltipRow label="Total" value={formatMoney(total, currency)} total />
      )}
    </TooltipBox>
  );
}
