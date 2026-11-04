"use client";

import type { Cloud, CloudInfo, Summary } from "@/lib/api";
import { formatCount, formatDay, formatMoney, formatShare } from "@/lib/format";
import { CloudBadge, cloudLabel } from "./CloudBadge";
import { accountNoun, cloudColor } from "./CloudFilter";
import { ui } from "./ui";
import styles from "./SpendByCloud.module.css";

function plural(n: number, noun: string): string {
  return `${formatCount(n)} ${noun}${n === 1 ? "" : "s"}`;
}

/**
 * Azure and Google Cloud side by side for the range: one share bar for the split, then a tile
 * per cloud with its total, share and tokens. Totals are already in the reporting currency.
 */
export function SpendByCloud({
  summary,
  clouds,
  start,
  end,
  onSelect,
}: {
  summary: Summary;
  clouds: CloudInfo[];
  start: string;
  end: string;
  onSelect: (cloud: Cloud) => void;
}) {
  const currency = summary.currency || "USD";
  const approx = summary.fx_approximate === true;
  const info = new Map(clouds.map((c) => [c.cloud, c]));
  const rows = (summary.by_cloud ?? []).map((c) => ({ ...c, total: Number(c.total_cost) }));
  // Clouds with accounts but no spend yet still get a tile, so a new project shows up at once.
  for (const c of clouds) {
    if (!rows.some((r) => r.cloud === c.cloud)) {
      rows.push({
        cloud: c.cloud,
        billed_cost: "0",
        estimated_cost: "0",
        total_cost: "0",
        input_tokens: 0,
        output_tokens: 0,
        total: 0,
      });
    }
  }
  const grand = rows.reduce((s, r) => s + r.total, 0);
  const shareOf = (v: number) => (grand > 0 ? v / grand : 0);
  const money = (v: number) => `${approx ? "≈ " : ""}${formatMoney(v, currency)}`;
  const barLabel =
    "Share of spend: " +
    rows.map((r) => `${cloudLabel(r.cloud)} ${formatShare(shareOf(r.total))}`).join(", ");

  return (
    <section className={ui.panel} aria-labelledby="spend-by-cloud">
      <div className={ui.panelHead}>
        <h2 className={ui.panelTitle} id="spend-by-cloud">
          Spend by cloud
        </h2>
        <span className={ui.cellSub}>
          {formatDay(start)} – {formatDay(end)} · total {money(grand)}
        </span>
      </div>
      {grand > 0 && (
        <div className={styles.bar} role="img" aria-label={barLabel}>
          {rows
            .filter((r) => r.total > 0)
            .map((r) => (
              <div
                key={r.cloud}
                className={styles.segment}
                style={{
                  width: `${shareOf(r.total) * 100}%`,
                  background: cloudColor(r.cloud) ?? "var(--series-other)",
                }}
                title={`${cloudLabel(r.cloud)}: ${formatMoney(r.total, currency)}`}
              />
            ))}
        </div>
      )}
      <ul className={styles.tiles}>
        {rows.map((r) => {
          const c = info.get(r.cloud);
          const tokens = r.input_tokens + r.output_tokens;
          const facts = [
            tokens > 0 ? `${formatCount(tokens, true)} tokens` : "No token data",
            c ? plural(c.tracked_accounts, `tracked ${accountNoun(r.cloud)}`) : null,
            c ? plural(c.deployments, "deployment") : null,
          ].filter(Boolean);
          return (
            <li key={r.cloud} className={styles.tile}>
              <div className={styles.tileHead}>
                <CloudBadge cloud={r.cloud} />
                <span className={styles.share}>
                  <span
                    className={styles.swatch}
                    style={{ background: cloudColor(r.cloud) ?? "var(--series-other)" }}
                    aria-hidden
                  />
                  {formatShare(shareOf(r.total))}
                  <span className={ui.srOnly}> of total spend</span>
                </span>
              </div>
              <div className={styles.total} title={tokens > 0 ? `${formatCount(tokens)} tokens` : undefined}>
                {money(r.total)}
              </div>
              <p className={styles.facts}>{facts.join(" · ")}</p>
              {(r.cloud === "azure" || r.cloud === "gcp" || r.cloud === "aws") && (
                <button
                  type="button"
                  className={styles.only}
                  onClick={() => onSelect(r.cloud as Cloud)}
                >
                  Show only {cloudLabel(r.cloud)} →
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
