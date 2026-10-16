"use client";

import { useState } from "react";
import { downloadFile, type Query } from "@/lib/api";
import { useSession } from "./AuthProvider";
import { errorMessage } from "./ui";
import styles from "./ExportButton.module.css";

export type ExportName =
  | "costs-daily"
  | "usage-daily"
  | "deployments"
  | "deployment-daily"
  | "summary"
  | "cost-centres-daily";

/**
 * Saves /api/v1/exports/<name>.csv with the same params as the view it sits on. Fetched with
 * the session token (a plain link would go unauthenticated), so it works with or without auth.
 */
export function ExportButton({
  name,
  query,
  label = "Export CSV",
}: {
  name: ExportName;
  query?: Query;
  label?: string;
}) {
  const { api } = useSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await downloadFile(`/api/v1/exports/${name}.csv`, {
        getToken: api.getToken,
        query,
        filename: `${name}.csv`,
      });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className={styles.wrap}>
      <button type="button" className={styles.button} onClick={() => void run()} disabled={busy}>
        {busy ? "Exporting…" : label}
      </button>
      {error && (
        <span className={styles.error} role="alert">
          {error}
        </span>
      )}
    </span>
  );
}
