"use client";

import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatMoney } from "@/lib/format";
import { TooltipBox, TooltipRow, chartStyles as styles, xAxisProps, yAxisBase } from "./chartParts";

export interface RatePoint {
  day: string;
  rate: number | null; // null on days without tokens: a gap, not a zero
  estimated: boolean;
}

/** $ per 1M tokens over time; one series, so the panel title names it and there's no legend. */
export function RateChart({
  points,
  currency,
  height = 240,
}: {
  points: RatePoint[];
  currency: string;
  height?: number;
}) {
  return (
    <div className={styles.wrap}>
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={points} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis {...xAxisProps} />
          <YAxis {...yAxisBase} tickFormatter={(v: number) => formatMoney(v, currency, true)} />
          <Tooltip
            cursor={{ stroke: "var(--axis)", strokeWidth: 1 }}
            content={({ active, payload }) => {
              const p = payload?.[0]?.payload as RatePoint | undefined;
              if (!active || !p) return null;
              return (
                <TooltipBox day={p.day} estimated={p.estimated}>
                  <TooltipRow
                    color="var(--series-1)"
                    label="Per 1M tokens"
                    value={p.rate === null ? "No tokens" : formatMoney(p.rate, currency)}
                  />
                </TooltipBox>
              );
            }}
          />
          <Line
            type="monotone"
            dataKey="rate"
            stroke="var(--series-1)"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
            dot={false}
            activeDot={{ r: 4, fill: "var(--series-1)", stroke: "var(--surface-1)", strokeWidth: 2 }}
            connectNulls={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
