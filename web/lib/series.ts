import type { DailyCostRow } from "./api";

// Seven validated categorical slots; anything past that folds into "Other" (never a new hue).
const SLOTS = 7;
export const OTHER = "Other";

export interface Series {
  key: string;
  color: string;
}

export interface DailyPoint {
  day: string;
  [model: string]: number | string;
}

/**
 * Pivot day x model rows into one point per day, keeping the costliest models as their own
 * series. Every day in the range is present so gaps read as zero spend, not missing bars.
 */
export function pivotDaily(
  rows: DailyCostRow[],
  days: string[],
): { series: Series[]; points: DailyPoint[] } {
  const totals = new Map<string, number>();
  for (const r of rows) totals.set(r.model, (totals.get(r.model) ?? 0) + Number(r.billed_cost));
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([m]) => m);
  const named = ranked.length > SLOTS ? ranked.slice(0, SLOTS - 1) : ranked;
  const hasOther = ranked.length > named.length;

  const series: Series[] = named.map((key, i) => ({ key, color: `var(--series-${i + 1})` }));
  if (hasOther) series.push({ key: OTHER, color: "var(--series-other)" });

  const byDay = new Map<string, DailyPoint>();
  for (const day of days) {
    const point: DailyPoint = { day };
    for (const s of series) point[s.key] = 0;
    byDay.set(day, point);
  }
  const namedSet = new Set(named);
  for (const r of rows) {
    const point = byDay.get(r.day);
    if (!point) continue;
    const key = namedSet.has(r.model) ? r.model : OTHER;
    point[key] = (point[key] as number) + Number(r.billed_cost);
  }
  return { series, points: [...byDay.values()] };
}

/** Every YYYY-MM-DD from start to end inclusive. */
export function daysBetween(start: string, end: string): string[] {
  const out: string[] = [];
  const d = new Date(`${start}T00:00:00Z`);
  const last = new Date(`${end}T00:00:00Z`);
  while (d <= last) {
    out.push(d.toISOString().slice(0, 10));
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return out;
}
