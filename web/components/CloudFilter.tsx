"use client";

import type { Cloud, CloudInfo } from "@/lib/api";
import { useSession } from "./AuthProvider";
import { cloudLabel } from "./CloudBadge";
import { Segmented, useLoad } from "./ui";
import styles from "./CloudFilter.module.css";

export { styles as cloudFilterStyles };

/** "all" means no cloud filter; otherwise every request on the page carries `cloud=<value>`. */
export type CloudChoice = "all" | Cloud;

/** Query params for a choice; spread into API calls and export queries alike. */
export function cloudQuery(choice: CloudChoice): { cloud?: Cloud } {
  return choice === "all" ? {} : { cloud: choice };
}

/** What an account is called on each cloud, singular. */
export function accountNoun(cloud: string | null): string {
  if (cloud === "azure") return "subscription";
  if (cloud === "gcp") return "project";
  return "account";
}

/**
 * Fixed hue per cloud (same families as CloudBadge), so a cloud keeps its colour across the
 * share bar and the daily chart whichever one costs more.
 */
export function cloudColor(cloud: string): string | undefined {
  if (cloud === "azure") return "var(--series-1)";
  if (cloud === "gcp") return "var(--series-3)";
  if (cloud === "aws") return "var(--series-4)";
  return undefined;
}

function isCloud(v: string): v is Cloud {
  return v === "azure" || v === "gcp" || v === "aws";
}

/**
 * Clouds from /clouds. A failed call reads as "no clouds" so the page falls back to its
 * single-cloud layout instead of showing an error for an optional control.
 */
export function useClouds(): { clouds: CloudInfo[]; ready: boolean } {
  const { api } = useSession();
  const [state] = useLoad(() => api.clouds(), "clouds");
  return {
    clouds: state.status === "ready" ? state.data.filter((c) => isCloud(c.cloud)) : [],
    ready: state.status !== "loading",
  };
}

/** All / Azure / Google Cloud; renders nothing unless there is more than one cloud to pick. */
export function CloudFilter({
  clouds,
  value,
  onChange,
}: {
  clouds: CloudInfo[];
  value: CloudChoice;
  onChange: (v: CloudChoice) => void;
}) {
  if (clouds.length < 2) return null;
  const options: { value: CloudChoice; label: string }[] = [
    { value: "all", label: "All clouds" },
    ...clouds.map((c) => ({ value: c.cloud as Cloud, label: c.label || cloudLabel(c.cloud) })),
  ];
  return (
    <div className={styles.scrollX}>
      <Segmented label="Cloud" options={options} value={value} onChange={onChange} />
    </div>
  );
}

/**
 * States the reporting currency when clouds are mixed, and flags approximate conversions with
 * "≈" so a stale exchange rate is never silent.
 */
export function CurrencyNote({
  currency,
  multiCloud,
  approximate,
}: {
  currency: string;
  multiCloud: boolean;
  approximate: boolean;
}) {
  if (!multiCloud && !approximate) return null;
  return (
    <p className={styles.currencyNote}>
      {multiCloud && (
        <span>
          All amounts in {currency}, converted from each cloud&apos;s billing currency at daily
          exchange rates.
        </span>
      )}
      {approximate && (
        <span className={styles.approx}>
          <span className={styles.approxMark} aria-hidden>
            ≈
          </span>
          Some amounts are approximate: a recent exchange rate wasn&apos;t available, so the
          nearest one was used.
        </span>
      )}
    </p>
  );
}
