"use client";

import { useMemo, useState } from "react";
import { lastNDays, type DateRange } from "@/lib/api";
import { AllocApi, resourceName, toNumber, type PtuDeployment } from "@/lib/api-m2-alloc";
import { formatCount, formatMoney } from "@/lib/format";
import { useSession } from "../AuthProvider";
import m2 from "../allocation/m2.module.css";
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
import { PtuHeatmap } from "./PtuHeatmap";

const keyOf = (d: PtuDeployment) => `${d.resource_id}|${d.deployment}`;

function Meter({ value, label }: { value: number | null; label: string }) {
  if (value === null) return <span className={ui.cellSub}>—</span>;
  const pct = Math.max(0, Math.min(1, value)) * 100;
  return (
    <div className={m2.meter} role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)} aria-label={label}>
      <div className={m2.meterTrack}>
        <div className={m2.meterFill} style={{ width: `${pct}%` }} />
      </div>
      <span className={m2.meterValue}>{Math.round(pct)}%</span>
    </div>
  );
}

/** PTU utilisation and idle-capacity report, costliest idle capacity first. */
export function PtuReport() {
  const { api } = useSession();
  const alloc = useMemo(() => new AllocApi(api), [api]);
  const [days, setDays] = useState<RangeDays>(30);
  const range = useMemo(() => lastNDays(days), [days]);
  const [state, retry] = useLoad(() => alloc.ptu(range), `${range.start}|${range.end}`);
  const [selected, setSelected] = useState<string | null>(null);

  const rows = useMemo(
    () =>
      state.status === "ready"
        ? [...state.data.deployments].sort((a, b) => Number(b.idle_cost) - Number(a.idle_cost))
        : [],
    [state],
  );
  const currency = rows[0]?.currency || "USD";
  const totalCost = rows.reduce((s, r) => s + Number(r.ptu_cost), 0);
  const idleCost = rows.reduce((s, r) => s + Number(r.idle_cost), 0);
  const capacity = rows.reduce((s, r) => s + (r.capacity ?? 0), 0);
  const current = rows.find((r) => keyOf(r) === selected) ?? null;

  return (
    <main className={`${ui.main} ${m2.root}`}>
      <div className={ui.toolbar}>
        <h1 className={ui.title}>PTU utilisation</h1>
        <Segmented label="Date range" options={RANGE_OPTIONS} value={days} onChange={setDays} />
      </div>

      {state.status === "loading" && (
        <>
          <KpiSkeletons count={3} />
          <Skeleton height={240} label="Loading PTU deployments…" />
        </>
      )}
      {state.status === "error" && (
        <ErrorNotice title="Couldn't load PTU utilisation" message={state.message} onRetry={retry} />
      )}
      {state.status === "ready" && rows.length === 0 && (
        <EmptyNotice title="No provisioned deployments">
          Only provisioned (PTU) deployments appear here; standard, global and data-zone deployments
          are billed per token and have no reserved capacity to sit idle. PTU deployments show up after
          the next inventory and metrics sync.
        </EmptyNotice>
      )}
      {rows.length > 0 && (
        <>
          <section className={ui.kpis} aria-label="Key figures">
            <Kpi label="PTU cost" value={formatMoney(totalCost, currency)} note={`${formatCount(capacity)} PTUs across ${rows.length} deployment${rows.length === 1 ? "" : "s"}`} />
            <Kpi
              label="Idle cost"
              value={formatMoney(idleCost, currency)}
              note={totalCost > 0 ? `${((idleCost / totalCost) * 100).toFixed(1)}% of PTU cost` : undefined}
            />
            <Kpi
              label="Idle hours"
              value={formatCount(rows.reduce((s, r) => s + r.idle_hours, 0))}
              note={`Of ${formatCount(rows.reduce((s, r) => s + r.hours, 0))} deployment-hours`}
            />
          </section>

          <section className={ui.panel}>
            <h2 className={ui.panelTitle}>Provisioned deployments</h2>
            <p className={ui.panelSub}>Sorted by idle cost. Select a deployment for its hourly utilisation.</p>
            <div className={ui.tableWrap}>
              <table className={ui.table}>
                <thead>
                  <tr>
                    <th scope="col">Deployment</th>
                    <th scope="col" className={ui.num}>
                      PTUs
                    </th>
                    <th scope="col">Average</th>
                    <th scope="col">Peak</th>
                    <th scope="col" className={ui.num}>
                      Idle hours
                    </th>
                    <th scope="col" className={ui.num}>
                      PTU cost
                    </th>
                    <th scope="col" className={ui.num}>
                      Idle cost
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const k = keyOf(r);
                    const isSel = k === selected;
                    return (
                      <tr
                        key={k}
                        className={`${m2.clickRow} ${isSel ? m2.selectedRow : ""}`}
                        onClick={() => setSelected(isSel ? null : k)}
                      >
                        <th scope="row">
                          <button
                            type="button"
                            className={m2.rowButton}
                            aria-expanded={isSel}
                            onClick={(e) => {
                              e.stopPropagation();
                              setSelected(isSel ? null : k);
                            }}
                          >
                            {r.deployment}
                          </button>
                          <div className={ui.cellSub}>
                            {[r.model, resourceName(r.resource_id)].filter(Boolean).join(" · ")}
                          </div>
                        </th>
                        <td className={ui.num}>{r.capacity ?? "—"}</td>
                        <td>
                          <Meter value={toNumber(r.avg_utilization)} label={`Average utilisation of ${r.deployment}`} />
                        </td>
                        <td>
                          <Meter value={toNumber(r.peak_utilization)} label={`Peak utilisation of ${r.deployment}`} />
                        </td>
                        <td className={ui.num}>
                          {formatCount(r.idle_hours)}
                          <span className={ui.cellSub}> / {formatCount(r.hours)}</span>
                        </td>
                        <td className={ui.num}>{formatMoney(Number(r.ptu_cost), r.currency || currency)}</td>
                        <td className={ui.num}>{formatMoney(Number(r.idle_cost), r.currency || currency)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          {current && <HourlyPanel alloc={alloc} deployment={current} range={range} />}
        </>
      )}
    </main>
  );
}

function HourlyPanel({
  alloc,
  deployment,
  range,
}: {
  alloc: AllocApi;
  deployment: PtuDeployment;
  range: DateRange;
}) {
  const [state, retry] = useLoad(
    () => alloc.ptuHourly(deployment, range),
    `${keyOf(deployment)}|${range.start}|${range.end}`,
  );
  return (
    <section className={ui.panel} aria-labelledby="ptu-hourly-title">
      <h2 id="ptu-hourly-title" className={ui.panelTitle}>
        Hourly utilisation · {deployment.deployment}
      </h2>
      <p className={ui.panelSub}>Each cell is one UTC hour, shaded by how much of the reserved capacity was used.</p>
      {state.status === "loading" && <Skeleton height={260} label="Loading hourly utilisation…" />}
      {state.status === "error" && (
        <ErrorNotice title="Couldn't load hourly utilisation" message={state.message} onRetry={retry} />
      )}
      {state.status === "ready" &&
        (state.data.rows.length === 0 ? (
          <EmptyNotice title="No utilisation data in this range">
            Utilisation metrics arrive with the usage sync; check back after it runs.
          </EmptyNotice>
        ) : (
          <PtuHeatmap rows={state.data.rows} start={range.start} end={range.end} />
        ))}
    </section>
  );
}
