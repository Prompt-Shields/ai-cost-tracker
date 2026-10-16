"use client";

import type { ReactNode } from "react";
import { formatDay } from "@/lib/format";
import styles from "./DailyCostChart.module.css";

export { styles as chartStyles };

/**
 * Fill opacity for estimated (not yet billed) segments, from a theme token so dark mode can
 * step it up. The legend and tooltip also say "estimated" in words.
 */
export const ESTIMATED_OPACITY = "var(--estimated-opacity)";

export const xAxisProps = {
  dataKey: "day",
  tickFormatter: formatDay,
  tick: { fill: "var(--text-muted)", fontSize: 12 },
  axisLine: { stroke: "var(--axis)" },
  tickLine: false,
  minTickGap: 16,
} as const;

export const yAxisBase = {
  tick: { fill: "var(--text-muted)", fontSize: 12 },
  axisLine: false,
  tickLine: false,
  width: 56,
} as const;

export interface LegendEntry {
  label: string;
  color: string;
  kind?: "swatch" | "line";
}

/** HTML legend so long names truncate cleanly and the estimated note sits beside the series. */
export function ChartLegend({
  entries,
  estimatedNote,
}: {
  entries: LegendEntry[];
  estimatedNote?: boolean;
}) {
  if (entries.length === 0 && !estimatedNote) return null;
  return (
    <ul className={styles.legend}>
      {entries.map((e) => (
        <li key={e.label} className={styles.legendItem}>
          <span
            className={e.kind === "line" ? styles.lineKey : styles.swatch}
            style={{ background: e.color }}
            aria-hidden
          />
          <span className={styles.legendLabel} title={e.label}>
            {e.label}
          </span>
        </li>
      ))}
      {estimatedNote && (
        <li className={styles.legendNote}>
          <span
            className={`${styles.swatch} ${styles.swatchEstimated}`}
            style={{ background: "var(--text-secondary)" }}
            aria-hidden
          />
          Lighter bars: estimated, not yet billed
        </li>
      )}
    </ul>
  );
}

export function TooltipBox({
  day,
  estimated,
  children,
}: {
  day: string;
  estimated?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={styles.tooltip}>
      <div className={styles.tooltipTitle}>
        {formatDay(day)}
        {estimated ? " · estimated" : ""}
      </div>
      {children}
    </div>
  );
}

export function TooltipRow({
  color,
  label,
  value,
  total,
  estimated,
}: {
  color?: string;
  label: string;
  value: string;
  total?: boolean;
  estimated?: boolean;
}) {
  return (
    <div className={`${styles.tooltipRow} ${total ? styles.tooltipTotal : ""}`}>
      {color ? (
        <span
          className={`${styles.swatch} ${estimated ? styles.swatchEstimated : ""}`}
          style={{ background: color }}
        />
      ) : (
        <span />
      )}
      <span className={styles.tooltipLabel}>
        {label}
        {estimated ? " (est.)" : ""}
      </span>
      <span className={styles.tooltipValue}>{value}</span>
    </div>
  );
}
