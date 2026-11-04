import Link from "next/link";
import styles from "./connect.module.css";

const CLOUDS = [
  { cloud: "azure", label: "Azure", href: "/connect" },
  { cloud: "gcp", label: "Google Cloud", href: "/connect/gcp" },
] as const;

export type ConnectCloud = (typeof CLOUDS)[number]["cloud"];

/** Top-of-page switch between the connect flows; plain links so each flow has its own URL. */
export function CloudSwitcher({ active }: { active: ConnectCloud }) {
  return (
    <nav className={styles.cloudSwitch} aria-label="Cloud to connect">
      {CLOUDS.map((c) => (
        <Link
          key={c.cloud}
          href={c.href}
          className={c.cloud === active ? styles.cloudTabActive : styles.cloudTab}
          aria-current={c.cloud === active ? "page" : undefined}
        >
          {c.label}
        </Link>
      ))}
    </nav>
  );
}
