"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatDay, formatMoney } from "@/lib/format";
import type { DailyPoint, Series } from "@/lib/series";
import styles from "./DailyCostChart.module.css";

interface Props {
  points: DailyPoint[];
  series: Series[];
  currency: string;
}

export function DailyCostChart({ points, series, currency }: Props) {
  return (
    <div className={styles.wrap}>
      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis
            dataKey="day"
            tickFormatter={formatDay}
            tick={{ fill: "var(--text-muted)", fontSize: 12 }}
            axisLine={{ stroke: "var(--axis)" }}
            tickLine={false}
            minTickGap={16}
          />
          <YAxis
            tickFormatter={(v: number) => formatMoney(v, currency, true)}
            tick={{ fill: "var(--text-muted)", fontSize: 12 }}
            axisLine={false}
            tickLine={false}
            width={56}
          />
          <Tooltip
            cursor={{ fill: "var(--wash)" }}
            content={({ active, payload, label }) => (
              <ChartTooltip
                active={active}
                point={payload?.[0]?.payload as DailyPoint | undefined}
                label={label}
                currency={currency}
                series={series}
              />
            )}
          />
          <Legend
            iconType="square"
            iconSize={10}
            wrapperStyle={{ fontSize: 13, paddingTop: 8 }}
            formatter={(value: string) => <span className={styles.legendText}>{value}</span>}
          />
          {series.map((s, i) => (
            <Bar
              key={s.key}
              dataKey={s.key}
              name={s.key}
              stackId="cost"
              fill={s.color}
              maxBarSize={24}
              // Surface-coloured edge keeps a visible gap between stacked segments.
              stroke="var(--surface-1)"
              strokeWidth={1}
              radius={i === series.length - 1 ? [4, 4, 0, 0] : 0}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

interface TooltipProps {
  active?: boolean;
  point?: DailyPoint;
  label?: string | number;
  currency: string;
  series: Series[];
}

function ChartTooltip({ active, point, label, currency, series }: TooltipProps) {
  if (!active || !point) return null;
  const items = series
    .map((s) => ({ ...s, value: Number(point[s.key] ?? 0) }))
    .filter((s) => s.value > 0)
    .reverse(); // Match the visual stack order (top segment first).
  const total = items.reduce((sum, s) => sum + s.value, 0);
  return (
    <div className={styles.tooltip}>
      <div className={styles.tooltipTitle}>{formatDay(String(label))}</div>
      {items.map((s) => (
        <div key={s.key} className={styles.tooltipRow}>
          <span className={styles.swatch} style={{ background: s.color }} />
          <span className={styles.tooltipLabel}>{s.key}</span>
          <span className={styles.tooltipValue}>{formatMoney(s.value, currency)}</span>
        </div>
      ))}
      <div className={`${styles.tooltipRow} ${styles.tooltipTotal}`}>
        <span />
        <span className={styles.tooltipLabel}>Total</span>
        <span className={styles.tooltipValue}>{formatMoney(total, currency)}</span>
      </div>
    </div>
  );
}
