"use client";

import { useMemo, useState } from "react";
import { toNumber, type PtuHourlyRow } from "@/lib/api-m2-alloc";
import { formatDay } from "@/lib/format";
import { daysBetween } from "../dailySeries";
import m2 from "../allocation/m2.module.css";
import { ui } from "../ui";

const STEPS = 7;
const HOURS = Array.from({ length: 24 }, (_, h) => h);

function stepOf(u: number): number {
  return Math.max(0, Math.min(STEPS - 1, Math.floor(u * STEPS)));
}

function pct(u: number): string {
  return `${Math.round(u * 100)}%`;
}

/**
 * Day x hour-of-day heat grid of PTU utilisation (UTC), one sequential hue: idle hours read as
 * pale cells and daily patterns line up in columns. Hours without data stay outlined, not zero.
 */
export function PtuHeatmap({ rows, start, end }: { rows: PtuHourlyRow[]; start: string; end: string }) {
  const [hover, setHover] = useState<{ day: string; hour: number; u: number | null } | null>(null);

  const byHour = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of rows) {
      const u = toNumber(r.utilization);
      const t = new Date(r.hour);
      if (u === null || Number.isNaN(t.getTime())) continue;
      m.set(t.toISOString().slice(0, 13), u);
    }
    return m;
  }, [rows]);
  const days = useMemo(() => daysBetween(start, end), [start, end]);
  const cellHeight = days.length > 45 ? 6 : days.length > 14 ? 10 : 18;
  const showEvery = days.length > 45 ? 14 : days.length > 14 ? 7 : 1;

  return (
    <div>
      <p className={m2.readout} aria-live="polite">
        {hover ? (
          <>
            <strong>{hover.u === null ? "No data" : pct(hover.u)}</strong> · {formatDay(hover.day)}{" "}
            {String(hover.hour).padStart(2, "0")}:00 UTC
          </>
        ) : (
          "Hover a cell for the hour's utilisation"
        )}
      </p>
      <div
        className={m2.heat}
        role="img"
        aria-label="Hourly utilisation heat map: rows are days, columns are UTC hours. A daily table follows."
      >
        <span aria-hidden />
        {HOURS.map((h) => (
          <span key={h} className={m2.heatHour} aria-hidden>
            {h % 6 === 0 ? String(h).padStart(2, "0") : ""}
          </span>
        ))}
        {days.map((day, i) => (
          <Row
            key={day}
            day={day}
            label={i % showEvery === 0 || i === days.length - 1 ? formatDay(day) : ""}
            byHour={byHour}
            height={cellHeight}
            onHover={setHover}
          />
        ))}
      </div>
      <div className={m2.heatLegend}>
        <span>0%</span>
        <span className={m2.ramp} aria-hidden>
          {Array.from({ length: STEPS }, (_, s) => (
            <span key={s} style={{ background: `var(--seq-${s})` }} />
          ))}
        </span>
        <span>100%</span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span className={`${m2.cell} ${m2.cellEmpty}`} style={{ width: 18, minHeight: 10 }} aria-hidden />
          No data
        </span>
      </div>
      <details className={ui.details} style={{ marginTop: 12 }}>
        <summary>Daily table</summary>
        <div className={ui.tableWrap}>
          <table className={ui.table}>
            <thead>
              <tr>
                <th scope="col">Day (UTC)</th>
                <th scope="col" className={ui.num}>Average</th>
                <th scope="col" className={ui.num}>Peak</th>
                <th scope="col" className={ui.num}>Hours with data</th>
              </tr>
            </thead>
            <tbody>
              {days.map((day) => {
                const vals = HOURS.map((h) => byHour.get(`${day}T${String(h).padStart(2, "0")}`)).filter(
                  (v): v is number => v !== undefined,
                );
                return (
                  <tr key={day}>
                    <th scope="row">{formatDay(day)}</th>
                    <td className={ui.num}>
                      {vals.length ? pct(vals.reduce((a, b) => a + b, 0) / vals.length) : "—"}
                    </td>
                    <td className={ui.num}>{vals.length ? pct(Math.max(...vals)) : "—"}</td>
                    <td className={ui.num}>{vals.length}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

function Row({
  day,
  label,
  byHour,
  height,
  onHover,
}: {
  day: string;
  label: string;
  byHour: Map<string, number>;
  height: number;
  onHover: (h: { day: string; hour: number; u: number | null } | null) => void;
}) {
  return (
    <>
      <span className={m2.heatDay} aria-hidden>
        {label}
      </span>
      {HOURS.map((h) => {
        const u = byHour.get(`${day}T${String(h).padStart(2, "0")}`) ?? null;
        const name = `${formatDay(day)} ${String(h).padStart(2, "0")}:00 UTC: ${u === null ? "no data" : pct(u)}`;
        return (
          <span
            key={h}
            title={name}
            className={`${m2.cell} ${u === null ? m2.cellEmpty : ""}`}
            style={{ minHeight: height, background: u === null ? undefined : `var(--seq-${stepOf(u)})` }}
            onMouseEnter={() => onHover({ day, hour: h, u })}
            onMouseLeave={() => onHover(null)}
          />
        );
      })}
    </>
  );
}
