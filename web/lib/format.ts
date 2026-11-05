export function formatMoney(value: number, currency: string, compact = false): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    notation: compact ? "compact" : "standard",
    maximumFractionDigits: compact ? 1 : 2,
    minimumFractionDigits: compact ? 0 : 2,
  }).format(value);
}

export function formatCount(value: number, compact = false): string {
  return new Intl.NumberFormat("en-US", {
    notation: compact ? "compact" : "standard",
    maximumFractionDigits: compact ? 1 : 0,
  }).format(value);
}

/** "2026-10-01" -> "Oct 1"; parsed as UTC so the label never shifts a day. */
export function formatDay(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

/** 0.4213 -> "42%"; shares under 1% say "<1%" so a small cloud never reads as zero. */
export function formatShare(fraction: number): string {
  if (!Number.isFinite(fraction) || fraction <= 0) return "0%";
  if (fraction < 0.01) return "<1%";
  return new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 0 }).format(
    fraction,
  );
}

/** "USD" -> "$", "EUR" -> "€": for column headers like "$/1M tokens" in any reporting currency. */
export function currencySymbol(currency: string): string {
  try {
    const parts = new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      currencyDisplay: "narrowSymbol",
    }).formatToParts(0);
    return parts.find((p) => p.type === "currency")?.value ?? currency;
  } catch {
    return currency;
  }
}

/**
 * Short, readable name for a resource id. GCP (`//aiplatform…/projects/<p>/locations/<l>`) and
 * AWS (`//bedrock…/accounts/<a>/regions/<r>`) read `<project|account>/<location|region>`;
 * Azure (and anything else) shows the last path segment, the Cognitive Services account.
 */
export function resourceLabel(resourceId: string): string {
  const scoped = /^\/\/(?:aiplatform\.googleapis\.com\/projects|bedrock\.amazonaws\.com\/accounts)\/([^/]+)\/(?:locations|regions)\/([^/]+)$/.exec(
    resourceId,
  );
  if (scoped) return `${scoped[1]}/${scoped[2]}`;
  const parts = resourceId.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? resourceId;
}

/** What the resource is on each cloud, for a "Resource" fact's label. */
export function resourceNoun(cloud: string): string {
  if (cloud === "aws") return "Account / region";
  if (cloud === "gcp") return "Project / location";
  return "Resource";
}

/** Unit of provisioned capacity: Azure PTUs, Bedrock model units. */
export function capacityUnit(cloud: string, n: number | null = null): string {
  const one = cloud === "aws" ? "model unit" : cloud === "gcp" ? "GSU" : "PTU";
  return n === 1 ? one : `${one}s`;
}

const SKU_LABELS: Record<string, string> = {
  provisionedthroughput: "Provisioned throughput",
  inferenceprofile: "Inference profile",
};

/**
 * Deployment SKU for display. Bedrock's SKUs read as words; an AWS/GCP deployment without a
 * SKU is pay-per-token ("On-demand"); provisioned capacity names its unit.
 */
export function skuLabel(cloud: string, sku: string | null, capacity: number | null): string | null {
  if (!sku) return cloud === "aws" || cloud === "gcp" ? "On-demand" : null;
  const name = SKU_LABELS[sku.replace(/\s+/g, "").toLowerCase()] ?? sku;
  if (capacity == null) return name;
  return cloud === "aws" ? `${name} × ${capacity} ${capacityUnit(cloud, capacity)}` : `${name} × ${capacity}`;
}
