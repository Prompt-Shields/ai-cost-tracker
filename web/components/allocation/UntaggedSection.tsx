"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { lastNDays } from "@/lib/api";
import type { AllocApi } from "@/lib/api-m2-alloc";
import { formatMoney } from "@/lib/format";
import { EmptyNotice, ErrorNotice, RANGE_OPTIONS, Segmented, Skeleton, ui, useLoad, type RangeDays } from "../ui";

/** Untagged-spend report: what each deployment is missing, so the bucket shrinks over time. */
export function UntaggedSection({ alloc }: { alloc: AllocApi }) {
  const [days, setDays] = useState<RangeDays>(30);
  const range = useMemo(() => lastNDays(days), [days]);
  const [state, retry] = useLoad(() => alloc.untagged(range), `untagged|${range.start}|${range.end}`);

  const rows = state.status === "ready" ? [...state.data.deployments].sort((a, b) => Number(b.cost) - Number(a.cost)) : [];
  const currency = state.status === "ready" ? state.data.currency || "USD" : "USD";

  return (
    <section className={ui.panel} aria-labelledby="untagged-title">
      <div className={ui.panelHead}>
        <h2 id="untagged-title" className={ui.panelTitle}>
          Untagged spend
        </h2>
        <Segmented label="Untagged report range" options={RANGE_OPTIONS} value={days} onChange={setDays} />
      </div>

      {state.status === "loading" && <Skeleton height={160} label="Loading untagged report…" />}
      {state.status === "error" && (
        <ErrorNotice title="Couldn't load the untagged report" message={state.message} onRetry={retry} />
      )}
      {state.status === "ready" && (
        <>
          <p className={ui.panelSub} style={{ marginTop: 0 }}>
            <strong style={{ color: "var(--text-primary)", fontSize: 18 }}>
              {formatMoney(Number(state.data.total), currency)}
            </strong>{" "}
            on {rows.length} deployment{rows.length === 1 ? "" : "s"} missing required tags in this range.
          </p>
          {rows.length === 0 ? (
            <EmptyNotice title="Everything is tagged">
              Every deployment with spend in this range carries the required tags.
            </EmptyNotice>
          ) : (
            <div className={ui.tableWrap}>
              <table className={ui.table}>
                <thead>
                  <tr>
                    <th scope="col">Deployment</th>
                    <th scope="col">Model</th>
                    <th scope="col">Missing tags</th>
                    <th scope="col" className={ui.num}>
                      Cost
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((d) => (
                    <tr key={d.id}>
                      <th scope="row">
                        <Link className={ui.link} href={`/deployments/${encodeURIComponent(d.id)}`}>
                          {d.name}
                        </Link>
                        {d.account_name && <div className={ui.cellSub}>{d.account_name}</div>}
                      </th>
                      <td>{d.model ?? "—"}</td>
                      <td>
                        <div className={ui.chips}>
                          {d.missing_tags.map((t) => (
                            <span key={t} className={ui.chip}>
                              {t}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td className={ui.num}>{formatMoney(Number(d.cost), currency)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <th scope="row" colSpan={3}>
                      Total untagged
                    </th>
                    <td className={ui.num}>{formatMoney(Number(state.data.total), currency)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}
