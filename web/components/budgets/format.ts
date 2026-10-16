import type { Deployment } from "@/lib/api";
import type { Budget, BudgetScopeType, CloudAccount } from "@/lib/api-m2-alerts";

export const SCOPE_TYPES: readonly { value: BudgetScopeType; label: string }[] = [
  { value: "all", label: "All AI spend" },
  { value: "account", label: "Subscription" },
  { value: "deployment", label: "Deployment" },
  { value: "model", label: "Model" },
  { value: "tag", label: "Tag" },
];

/**
 * Thresholds are stored as fractions (0.8); accept percents too in case a client sent them,
 * so 80 and 0.8 both read as 80%.
 */
export function thresholdPercents(thresholds: number[]): number[] {
  const nums = thresholds.map(Number).filter((n) => Number.isFinite(n));
  const fractions = nums.every((n) => n <= 5);
  return nums.map((n) => Math.round((fractions ? n * 100 : n) * 100) / 100).sort((a, b) => a - b);
}

/** "80, 100" -> [0.8, 1]; null when any entry is not a positive number up to 1000%. */
export function parseThresholds(text: string): number[] | null {
  const parts = text
    .split(",")
    .map((p) => p.trim().replace(/%$/, ""))
    .filter(Boolean);
  if (parts.length === 0) return [];
  const nums = parts.map(Number);
  if (nums.some((n) => !Number.isFinite(n) || n <= 0 || n > 1000)) return null;
  return [...new Set(nums)].sort((a, b) => a - b).map((n) => Math.round(n * 100) / 10000);
}

/** Percent of the budget used; falls back to MTD / amount if the API sends none. */
export function percentUsed(b: Budget): number | null {
  if (b.percent_used !== null && b.percent_used !== undefined && b.percent_used !== "") {
    const n = Number(b.percent_used);
    if (Number.isFinite(n)) return n;
  }
  const amount = Number(b.amount);
  return amount > 0 ? (Number(b.month_to_date) / amount) * 100 : null;
}

/** "<account resource name>/<deployment>": the same key the cost API groups deployments by. */
export function deploymentKey(d: Deployment): string {
  const resource = d.resource_id.split("/").filter(Boolean).pop() ?? d.resource_id;
  return `${resource}/${d.name}`;
}

export function scopeLabel(
  b: Pick<Budget, "scope_type" | "scope_value">,
  accounts: Map<string, CloudAccount>,
  deployments?: Map<string, Deployment>,
): { kind: string; value: string | null } {
  const v = b.scope_value;
  switch (b.scope_type) {
    case "all":
      return { kind: "All AI spend", value: null };
    case "account":
      return { kind: "Subscription", value: (v && accounts.get(v)?.name) || v };
    case "deployment": {
      // scope_value is a deployment id; show "<resource>/<deployment>" when we know it.
      const d = v ? deployments?.get(v) : undefined;
      return { kind: "Deployment", value: d ? deploymentKey(d) : v };
    }
    case "model":
      return { kind: "Model", value: v };
    case "tag":
      return { kind: "Tag", value: v };
    default:
      return { kind: String(b.scope_type), value: v };
  }
}
