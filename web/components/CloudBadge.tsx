import styles from "./CloudBadge.module.css";

/** Display names for the clouds the API reports (`cloud` field on accounts, deployments, rows). */
export const CLOUD_LABELS: Record<string, string> = {
  azure: "Azure",
  gcp: "Google Cloud",
  aws: "AWS",
};

export function cloudLabel(cloud: string): string {
  return CLOUD_LABELS[cloud] ?? cloud;
}

/** Small tag naming a cloud; colour is a hint only, the text always says which cloud. */
export function CloudBadge({ cloud }: { cloud: string }) {
  const tone = cloud === "azure" || cloud === "gcp" || cloud === "aws" ? styles[cloud] : "";
  return <span className={`${styles.badge} ${tone}`}>{cloudLabel(cloud)}</span>;
}
