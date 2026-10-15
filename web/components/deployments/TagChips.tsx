import { ui as styles } from "../ui";

export function TagChips({ tags }: { tags: Record<string, string> }) {
  const entries = Object.entries(tags);
  if (entries.length === 0) return <span className={styles.muted}>—</span>;
  return (
    <span className={styles.chips}>
      {entries.map(([k, v]) => (
        <span key={k} className={styles.chip}>
          {k}: {v}
        </span>
      ))}
    </span>
  );
}
