import { ApiError } from "@/lib/api";

/** "just now", "5 min ago", "3 h ago", "2 days ago"; relative so polling keeps it fresh. */
export function timeAgo(iso: string | null, now: number = Date.now()): string {
  if (!iso) return "never";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "unknown";
  const secs = Math.max(0, Math.round((now - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

export function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** User-facing text for an API or MSAL failure. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "Your session has expired. Reload the page to sign in again.";
    if (err.status === 403) return "You don't have permission for this. Ask an admin.";
    return err.message;
  }
  return err instanceof Error ? err.message : "Something went wrong.";
}

/** A fix that is a shell command gets a copy button; prose fixes are shown as text. */
export function isCommand(text: string): boolean {
  return /^(az|act)\s/.test(text.trim());
}
