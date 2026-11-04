"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import type { Deployment } from "@/lib/api";
import { useSession } from "../AuthProvider";
import { CloudBadge } from "../CloudBadge";
import { CloudFilter, cloudFilterStyles, cloudQuery, useClouds, type CloudChoice } from "../CloudFilter";
import { ExportButton } from "../ExportButton";
import { EmptyNotice, ErrorNotice, Skeleton, ui as styles, useLoad } from "../ui";
import { TagChips } from "./TagChips";

const CLOUD_ORDER = ["azure", "gcp", "aws"];
function cloudRank(cloud: string): number {
  const i = CLOUD_ORDER.indexOf(cloud);
  return i === -1 ? CLOUD_ORDER.length : i;
}

/** Every tracked deployment, grouped by account (subscription / project) so ownership reads at a glance. */
export function DeploymentsList() {
  const { api } = useSession();
  const { clouds } = useClouds();
  const multiCloud = clouds.length > 1;
  const [choice, setChoice] = useState<CloudChoice>("all");
  const cloud: CloudChoice =
    multiCloud && (choice === "all" || clouds.some((c) => c.cloud === choice)) ? choice : "all";
  const filters = cloudQuery(cloud);
  const [state, retry] = useLoad(() => api.deployments(filters), `deployments|${cloud}`);
  const [query, setQuery] = useState("");
  const showCloud = multiCloud && cloud === "all";

  const groups = useMemo(() => {
    if (state.status !== "ready") return [];
    const q = query.trim().toLowerCase();
    const match = (d: Deployment) =>
      !q ||
      [d.name, d.model, d.account_name, d.account_id, d.region, ...Object.entries(d.tags).flat()]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(q));
    // Keyed by cloud too: an account id is only unique within its cloud.
    const byAccount = new Map<string, { name: string; cloud: string; items: Deployment[] }>();
    for (const d of state.data.filter(match)) {
      const key = `${d.cloud}|${d.account_id}`;
      const g = byAccount.get(key) ?? {
        name: d.account_name || d.account_id,
        cloud: d.cloud,
        items: [],
      };
      g.items.push(d);
      byAccount.set(key, g);
    }
    return [...byAccount.entries()]
      .map(([id, g]) => ({ id, ...g, items: g.items.sort((a, b) => a.name.localeCompare(b.name)) }))
      // Clouds in /clouds order (Azure first), then accounts by name.
      .sort((a, b) => cloudRank(a.cloud) - cloudRank(b.cloud) || a.name.localeCompare(b.name));
  }, [state, query]);

  return (
    <main className={styles.main}>
      <div className={styles.toolbar}>
        <h1 className={styles.title}>Deployments</h1>
        <div className={`${styles.controls} ${cloudFilterStyles.shrink}`}>
          <CloudFilter clouds={clouds} value={cloud} onChange={setChoice} />
          {state.status === "ready" && state.data.length > 0 && (
            <>
              <label className={styles.srOnly} htmlFor="dep-filter">
                Filter deployments
              </label>
              <input
                id="dep-filter"
                type="search"
                className={styles.input}
                style={{ width: 220, maxWidth: "100%" }}
                placeholder="Filter by name, model, tag…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              <ExportButton name="deployments" query={filters} />
            </>
          )}
        </div>
      </div>

      {state.status === "loading" && <Skeleton height={320} label="Loading deployments…" />}
      {state.status === "error" && (
        <ErrorNotice title="Couldn't load deployments" message={state.message} onRetry={retry} />
      )}
      {state.status === "ready" && state.data.length === 0 && (
        <EmptyNotice title="No deployments yet">
          {cloud === "gcp"
            ? "Connect a Google Cloud project and its Vertex AI models show up here after the next inventory sync. "
            : "Connect an Azure subscription or Google Cloud project and its AI deployments show up here after the next inventory sync. "}
          <Link className={styles.link} href={cloud === "gcp" ? "/connect/gcp" : "/connect"}>
            Connect a cloud
          </Link>
        </EmptyNotice>
      )}
      {state.status === "ready" && state.data.length > 0 && groups.length === 0 && (
        <p className={styles.muted}>No deployments match “{query}”.</p>
      )}
      {groups.map((g) => (
        <section key={g.id} className={styles.panel}>
          <h2 className={styles.panelTitle} style={{ overflowWrap: "anywhere" }}>
            {g.name}
          </h2>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Deployment</th>
                  {showCloud && <th scope="col">Cloud</th>}
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
                    {showCloud && (
                      <td>
                        <CloudBadge cloud={d.cloud} />
                      </td>
                    )}
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
