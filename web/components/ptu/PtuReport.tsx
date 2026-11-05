"use client";

import { useMemo, useState } from "react";
import { lastNDays, type ApiClient, type DateRange } from "@/lib/api";
import { AllocApi, ptuKey, toNumber, type PtuDeployment, type PtuHourlyRow } from "@/lib/api-m2-alloc";
import { capacityUnit, formatCount, formatMoney, resourceLabel } from "@/lib/format";
import { useSession } from "../AuthProvider";
import { CloudBadge } from "../CloudBadge";
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

/**
 * /ptu row; the M4 fields are optional so an older API still renders. `utilization_reported`
 * false (AWS Bedrock provisioned throughput) means the cloud publishes no utilisation: show
 * cost and say so, never 0% or idle.
 */
type PtuRow = Omit<PtuDeployment, "idle_cost"> & {
  cloud?: string;
  deployment_label?: string;
  utilization_reported?: boolean;
  utilization_note?: string | null;
  idle_cost: string | null;
};

const keyOf = (d: PtuRow) => `${d.resource_id}|${d.deployment}`;
const reported = (d: PtuRow) => d.utilization_reported !== false;
const cloudOf = (d: PtuRow) => d.cloud ?? "azure";

/** "50 PTUs, 2 model units": capacity summed per unit, since clouds count it differently. */
function capacitySummary(rows: PtuRow[]): string {
  const byUnit = new Map<string, number>();
  for (const r of rows) {
    if (r.capacity == null) continue;
    const c = cloudOf(r);
    byUnit.set(c, (byUnit.get(c) ?? 0) + r.capacity);
  }
  return [...byUnit.entries()]
    .map(([cloud, n]) => `${formatCount(n)} ${capacityUnit(cloud, n)}`)
    .join(", ");
}

function NotReported({ row }: { row: PtuRow }) {
  return (
    <span className={ui.cellSub} title={row.utilization_note ?? undefined}>
      Not reported
    </span>
  );
}

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
        ? [...(state.data.deployments as PtuRow[])].sort(
            (a, b) =>
              Number(b.idle_cost ?? 0) - Number(a.idle_cost ?? 0) ||
              Number(b.ptu_cost) - Number(a.ptu_cost),
          )
        : [],
    [state],
  );
  const currency = rows[0]?.currency || "USD";
  const totalCost = rows.reduce((s, r) => s + Number(r.ptu_cost), 0);
  const idleCost = rows.reduce((s, r) => s + Number(r.idle_cost ?? 0), 0);
  // Idle share is only meaningful against the cost of deployments that report utilisation.
  const measuredCost = rows.reduce((s, r) => s + (reported(r) ? Number(r.ptu_cost) : 0), 0);
  const unreported = rows.filter((r) => !reported(r));
  const capacity = capacitySummary(rows);
  const multiCloud = new Set(rows.map(cloudOf)).size > 1;
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
          Only provisioned deployments appear here (Azure PTU, AWS Bedrock provisioned throughput);
          standard, global, data-zone and on-demand deployments are billed per token and have no
          reserved capacity to sit idle. They show up after the next inventory and metrics sync.
        </EmptyNotice>
      )}
      {rows.length > 0 && (
        <>
          <section className={ui.kpis} aria-label="Key figures">
            <Kpi
              label="PTU cost"
              value={formatMoney(totalCost, currency)}
              note={`${capacity ? `${capacity} across ` : ""}${rows.length} deployment${rows.length === 1 ? "" : "s"}`}
            />
            <Kpi
              label="Idle cost"
              value={formatMoney(idleCost, currency)}
              note={
                [
                  measuredCost > 0
                    ? `${((idleCost / measuredCost) * 100).toFixed(1)}% of ${unreported.length ? "measured " : ""}PTU cost`
                    : null,
                  unreported.length
                    ? `excludes ${unreported.length} without utilisation data`
                    : null,
                ]
                  .filter(Boolean)
                  .join("; ") || undefined
              }
            />
            <Kpi
              label="Idle hours"
              value={formatCount(rows.reduce((s, r) => s + r.idle_hours, 0))}
              note={`Of ${formatCount(rows.reduce((s, r) => s + r.hours, 0))} deployment-hours`}
            />
          </section>

          <section className={ui.panel}>
            <h2 className={ui.panelTitle}>Provisioned deployments</h2>
            <p className={ui.panelSub}>
              Sorted by idle cost. Select a deployment for its hourly utilisation.
              {unreported.length > 0 &&
                " AWS doesn't publish utilisation for Bedrock provisioned throughput, so those rows show cost only."}
            </p>
            <div className={ui.tableWrap}>
              <table className={ui.table}>
                <thead>
                  <tr>
                    <th scope="col">Deployment</th>
                    <th scope="col" className={ui.num}>
                      Capacity
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
                            {multiCloud && (
                              <>
                                <CloudBadge cloud={cloudOf(r)} />{" "}
                              </>
                            )}
                            {[r.model, resourceLabel(r.resource_id)].filter(Boolean).join(" · ")}
                          </div>
                        </th>
                        <td className={ui.num}>
                          {r.capacity ?? "—"}
                          {r.capacity != null && (
                            <div className={ui.cellSub}>{capacityUnit(cloudOf(r), r.capacity)}</div>
                          )}
                        </td>
                        <td>
                          {reported(r) ? (
                            <Meter value={toNumber(r.avg_utilization)} label={`Average utilisation of ${r.deployment}`} />
                          ) : (
                            <NotReported row={r} />
                          )}
                        </td>
                        <td>
                          {reported(r) ? (
                            <Meter value={toNumber(r.peak_utilization)} label={`Peak utilisation of ${r.deployment}`} />
                          ) : (
                            <NotReported row={r} />
                          )}
                        </td>
                        <td className={ui.num}>
                          {reported(r) ? (
                            <>
                              {formatCount(r.idle_hours)}
                              <span className={ui.cellSub}> / {formatCount(r.hours)}</span>
                            </>
                          ) : (
                            <span className={ui.cellSub}>—</span>
                          )}
                        </td>
                        <td className={ui.num}>{formatMoney(Number(r.ptu_cost), r.currency || currency)}</td>
                        <td className={ui.num}>
                          {r.idle_cost == null ? (
                            <span className={ui.cellSub} title={r.utilization_note ?? "No utilisation data in this range"}>
                              —
                            </span>
                          ) : (
                            formatMoney(Number(r.idle_cost), r.currency || currency)
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          {current && <HourlyPanel api={api} deployment={current} range={range} />}
        </>
      )}
    </main>
  );
}

/** Hourly path: the unique label when the API sends one (AWS/GCP keys can collide), else the key. */
function hourlyPath(d: PtuRow): string {
  const key = d.deployment_label ? encodeURIComponent(d.deployment_label) : ptuKey(d);
  return `/api/v1/ptu/${key}/hourly`;
}

function HourlyPanel({
  api,
  deployment,
  range,
}: {
  api: ApiClient;
  deployment: PtuRow;
  range: DateRange;
}) {
  const measured = reported(deployment);
  const [state, retry] = useLoad(
    () =>
      measured
        ? api.request<{ rows: PtuHourlyRow[] }>(hourlyPath(deployment), { query: { ...range } })
        : Promise.resolve({ rows: [] as PtuHourlyRow[] }),
    `${keyOf(deployment)}|${range.start}|${range.end}`,
  );
  if (!measured) {
    return (
      <section className={ui.panel} aria-labelledby="ptu-hourly-title">
        <h2 id="ptu-hourly-title" className={ui.panelTitle}>
          Hourly utilisation · {deployment.deployment}
        </h2>
        <EmptyNotice title={deployment.utilization_note ?? "Utilisation not reported"}>
          This cloud publishes no utilisation metric for provisioned capacity, so only its cost is
          shown. Compare the deployment&apos;s token usage on the Deployments page with the model
          units you reserved.
        </EmptyNotice>
      </section>
    );
  }
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
