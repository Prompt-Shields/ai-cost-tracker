"use client";

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatCount } from "@/lib/format";
import { ChartLegend, TooltipBox, TooltipRow, chartStyles as styles, xAxisProps, yAxisBase } from "./chartParts";

export interface TokenPoint {
  day: string;
  input: number;
  output: number;
}

const SERIES = [
  { key: "input", label: "Input tokens", color: "var(--series-1)" },
  { key: "output", label: "Output tokens", color: "var(--series-2)" },
] as const;

/** Input and output stacked: they're priced differently, so the split explains the cost. */
export function TokensChart({ points, height = 240 }: { points: TokenPoint[]; height?: number }) {
  return (
    <div className={styles.wrap}>
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis {...xAxisProps} />
          <YAxis {...yAxisBase} tickFormatter={(v: number) => formatCount(v, true)} />
          <Tooltip
            cursor={{ fill: "var(--wash)" }}
            content={({ active, payload }) => {
              const p = payload?.[0]?.payload as TokenPoint | undefined;
              if (!active || !p) return null;
              return (
                <TooltipBox day={p.day}>
                  {[...SERIES].reverse().map((s) => (
                    <TooltipRow key={s.key} color={s.color} label={s.label} value={formatCount(p[s.key])} />
                  ))}
                  <TooltipRow label="Total" value={formatCount(p.input + p.output)} total />
                </TooltipBox>
              );
            }}
          />
          {SERIES.map((s, i) => (
            <Bar
              key={s.key}
              dataKey={s.key}
              name={s.label}
              stackId="tokens"
              fill={s.color}
              maxBarSize={24}
              stroke="var(--surface-1)"
              strokeWidth={1}
              radius={i === SERIES.length - 1 ? [4, 4, 0, 0] : 0}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <ChartLegend entries={SERIES.map((s) => ({ label: s.label, color: s.color }))} />
    </div>
  );
}
