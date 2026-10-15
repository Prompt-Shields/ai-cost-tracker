import { shownCost, type DailyGroupRow } from "@/lib/api";

// Seven validated categorical slots; anything past that folds into "Other" (never a new hue).
const SLOTS = 7;
export const OTHER_KEY = "__other__";

export interface Series {
  key: string;
  label: string;
  color: string;
}

/** One bar per day: a value per series key, and which segments are still estimates. */
export interface DailyPoint {
  day: string;
  v: Record<string, number>;
  est: Record<string, boolean>;
}

/**
 * Pivot day x key rows into one point per day, keeping the costliest keys as their own series.
 * Every day in the range is present so gaps read as zero spend, not missing bars. A segment
 * counts as estimated when any row folded into it is, so "Other" never hides an estimate.
 */
export function pivotGrouped(
  rows: DailyGroupRow[],
  days: string[],
  labelFor: (key: string) => string = (k) => k,
): { series: Series[]; points: DailyPoint[] } {
  const totals = new Map<string, number>();
  for (const r of rows) totals.set(r.key, (totals.get(r.key) ?? 0) + shownCost(r));
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k);
  const named = ranked.length > SLOTS ? ranked.slice(0, SLOTS - 1) : ranked;
  const hasOther = ranked.length > named.length;

  const series: Series[] = named.map((key, i) => ({
    key,
    label: labelFor(key),
    color: `var(--series-${i + 1})`,
  }));
  if (hasOther) series.push({ key: OTHER_KEY, label: "Other", color: "var(--series-other)" });

  const byDay = new Map<string, DailyPoint>();
  for (const day of days) {
    const point: DailyPoint = { day, v: {}, est: {} };
    for (const s of series) point.v[s.key] = 0;
    byDay.set(day, point);
  }
  const namedSet = new Set(named);
  for (const r of rows) {
    const point = byDay.get(r.day);
    if (!point) continue;
    const key = namedSet.has(r.key) ? r.key : OTHER_KEY;
    point.v[key] = (point.v[key] ?? 0) + shownCost(r);
    if (r.day_status === "estimated") point.est[key] = true;
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
