"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import type { Deployment } from "@/lib/api";
import { useSession } from "../AuthProvider";
import { ExportButton } from "../ExportButton";
import { EmptyNotice, ErrorNotice, Skeleton, ui as styles, useLoad } from "../ui";
import { TagChips } from "./TagChips";

/** Every tracked deployment, grouped by subscription so ownership reads at a glance. */
export function DeploymentsList() {
  const { api } = useSession();
  const [state, retry] = useLoad(() => api.deployments(), "deployments");
  const [query, setQuery] = useState("");

  const groups = useMemo(() => {
    if (state.status !== "ready") return [];
    const q = query.trim().toLowerCase();
    const match = (d: Deployment) =>
      !q ||
      [d.name, d.model, d.account_name, d.account_id, d.region, ...Object.entries(d.tags).flat()]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(q));
    const byAccount = new Map<string, { name: string; items: Deployment[] }>();
    for (const d of state.data.filter(match)) {
      const g = byAccount.get(d.account_id) ?? { name: d.account_name || d.account_id, items: [] };
      g.items.push(d);
      byAccount.set(d.account_id, g);
    }
    return [...byAccount.entries()]
      .map(([id, g]) => ({ id, ...g, items: g.items.sort((a, b) => a.name.localeCompare(b.name)) }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [state, query]);

  return (
    <main className={styles.main}>
      <div className={styles.toolbar}>
        <h1 className={styles.title}>Deployments</h1>
        {state.status === "ready" && state.data.length > 0 && (
          <div className={styles.controls}>
            <label className={styles.srOnly} htmlFor="dep-filter">
              Filter deployments
            </label>
            <input
              id="dep-filter"
              type="search"
              className={styles.input}
              style={{ width: 220 }}
              placeholder="Filter by name, model, tag…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <ExportButton name="deployments" />
          </div>
        )}
      </div>

      {state.status === "loading" && <Skeleton height={320} label="Loading deployments…" />}
      {state.status === "error" && (
        <ErrorNotice title="Couldn't load deployments" message={state.message} onRetry={retry} />
      )}
      {state.status === "ready" && state.data.length === 0 && (
        <EmptyNotice title="No deployments yet">
          Connect an Azure subscription and its AI deployments show up here after the next
          inventory sync.{" "}
          <Link className={styles.link} href="/connect">
            Connect Azure
          </Link>
        </EmptyNotice>
      )}
      {state.status === "ready" && state.data.length > 0 && groups.length === 0 && (
        <p className={styles.muted}>No deployments match “{query}”.</p>
      )}
      {groups.map((g) => (
        <section key={g.id} className={styles.panel}>
          <h2 className={styles.panelTitle}>{g.name}</h2>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Deployment</th>
                  <th scope="col">Model</th>
                  <th scope="col">SKU</th>
                  <th scope="col">Region</th>
                  <th scope="col">Tags</th>
                </tr>
              </thead>
              <tbody>
                {g.items.map((d) => (
                  <tr key={d.id}>
                    <th scope="row">
                      <Link className={styles.link} href={`/deployments/${encodeURIComponent(d.id)}`}>
                        {d.name}
                      </Link>
                    </th>
                    <td>
                      {d.model ?? "—"}
                      {d.model_version && <span className={styles.muted}> {d.model_version}</span>}
                    </td>
                    <td>
                      {d.sku ?? "—"}
                      {d.capacity != null && <span className={styles.muted}> × {d.capacity}</span>}
                    </td>
                    <td>{d.region ?? "—"}</td>
                    <td>
                      <TagChips tags={d.tags} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </main>
  );
}
