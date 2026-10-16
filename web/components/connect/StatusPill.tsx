import type { AccessStatus, SourceStatus } from "@/lib/api-connect";
import styles from "./connect.module.css";

type Tone = "ok" | "stale" | "error" | "never" | "waiting";

const SOURCE: Record<SourceStatus, { tone: Tone; label: string }> = {
  ok: { tone: "ok", label: "OK" },
  stale: { tone: "stale", label: "Stale" },
  error: { tone: "error", label: "Error" },
  never: { tone: "never", label: "Not yet" },
};

const ACCESS: Record<AccessStatus, { tone: Tone; label: string }> = {
  connected: { tone: "ok", label: "Connected" },
  waiting_for_access: { tone: "waiting", label: "Waiting for access" },
  no_access: { tone: "error", label: "No access" },
};

function Pill({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span className={`${styles.pill} ${styles[tone]}`}>
      <span className={styles.dot} aria-hidden />
      {label}
    </span>
  );
}

export function SourcePill({ status }: { status: SourceStatus }) {
  return <Pill {...(SOURCE[status] ?? { tone: "never", label: status })} />;
}

export function AccessPill({ status }: { status: AccessStatus }) {
  return <Pill {...(ACCESS[status] ?? { tone: "never", label: status })} />;
}
