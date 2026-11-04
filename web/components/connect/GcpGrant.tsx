"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import type { GcpGrant as Grant, GcpProjectResult, GcpRole } from "@/lib/api-gcp";
import { CopyBlock } from "./CopyBlock";
import { CheckAgain } from "./ConnectGcp";
import { AccessPill } from "./StatusPill";
import { useGcpApi, usePoll } from "./hooks";
import styles from "./connect.module.css";

interface Data {
  grant: Grant;
  roles: GcpRole[];
}

/** The commands for one waiting project, on their own page so they're easy to find and share. */
export function GcpGrant({ projectId }: { projectId: string }) {
  const gcp = useGcpApi();
  const load = useCallback(async (): Promise<Data> => {
    const [grant, setup] = await Promise.all([
      gcp.grant(projectId),
      gcp.setup().catch(() => null), // Roles are context only; the commands matter.
    ]);
    return { grant, roles: (setup?.roles ?? []).filter((r) => !/billing|bigquery/i.test(r.role + r.scope)) };
  }, [gcp, projectId]);
  const { state, reload } = usePoll(load, null);

  return (
    <main className={`${styles.main} ${styles.narrow}`}>
      <div>
        <p className={styles.small}>
          <Link href="/connect/gcp" className={styles.link}>
            ← Connect Google Cloud
          </Link>
        </p>
        <h1 className={styles.title}>Grant read-only access to a project</h1>
        <p className={styles.lede}>
          AI Cost Tracker needs read-only roles on this Google Cloud project to show Vertex AI
          spend, tokens and deployments. Someone with Owner or Project IAM Admin on the project
          runs the commands below once.
        </p>
      </div>
      {state.status === "loading" && (
        <div className={`${styles.panel} ${styles.skeleton}`} style={{ height: 260 }} aria-busy="true">
          <span className={styles.srOnly}>Loading…</span>
        </div>
      )}
      {state.status === "error" && (
        <div className={styles.notice} role="alert">
          <p className={styles.noticeTitle}>Couldn&apos;t load the commands</p>
          <p className={styles.noticeBody}>{state.message}</p>
          <div className={styles.actions}>
            <button type="button" className={styles.secondary} onClick={reload}>
              Try again
            </button>
          </div>
        </div>
      )}
      {state.status === "ready" && <Body data={state.data} />}
    </main>
  );
}

function Body({ data: { grant, roles } }: { data: Data }) {
  const [result, setResult] = useState<GcpProjectResult | null>(null);
  const connected = result?.access_status === "connected";
  return (
    <>
      <section className={styles.panel} aria-live="polite">
        <div className={styles.resultHead}>
          <span style={{ minWidth: 0 }}>
            <span className={styles.subName}>{result?.name ?? grant.project_id}</span>
            <br />
            <span className={`${styles.mono} ${styles.muted}`}>{grant.project_id}</span>
          </span>
          {result && <AccessPill status={result.access_status} />}
        </div>
        {connected ? (
          <p className={styles.sourceBody}>
            Access is in place and data is loading. Nothing else to do.{" "}
            <Link href="/connections" className={styles.link}>
              See connection health
            </Link>
          </p>
        ) : (
          <p className={styles.sourceBody}>
            The project connects by itself within a few minutes after the commands run. Check now
            if you can&apos;t wait.
          </p>
        )}
        {!connected && (
          <div className={styles.actions} style={{ marginTop: 10 }}>
            <CheckAgain projectId={grant.project_id} onResult={setResult} />
          </div>
        )}
      </section>

      {!connected && (
        <section className={styles.panel}>
          <h2 className={styles.panelTitle}>Commands for the project owner</h2>
          <p className={styles.sourceBody} style={{ marginTop: 0 }}>
            Run in{" "}
            <a
              href="https://shell.cloud.google.com/"
              target="_blank"
              rel="noreferrer"
              className={styles.link}
            >
              Cloud Shell
            </a>{" "}
            or any terminal signed in with <code>gcloud auth login</code>. Copy and send them as
            they are; they contain no secrets.
          </p>
          <CopyBlock text={grant.commands} label={`Grant commands for ${grant.project_id}`} />
          {grant.service_account && (
            <p className={`${styles.muted} ${styles.small}`} style={{ marginTop: 10 }}>
              Prefer the console? IAM &amp; Admin → IAM → Grant access → principal{" "}
              <span className={styles.mono}>{grant.service_account}</span>, with the roles below.
            </p>
          )}
        </section>
      )}

      {roles.length > 0 && (
        <section className={styles.panel}>
          <h2 className={styles.panelTitle}>What is being asked</h2>
          <ul className={styles.roleList}>
            {roles.map((r) => (
              <li key={`${r.role}:${r.scope}`}>
                <span className={styles.mono}>{r.role}</span>
                <span className={styles.sourceBody} style={{ margin: 0 }}>
                  {r.why}
                </span>
              </li>
            ))}
          </ul>
          <p className={`${styles.muted} ${styles.small}`} style={{ marginTop: 10 }}>
            Read-only: it can&apos;t change, create or delete anything. Remove the roles at any
            time.
          </p>
        </section>
      )}
    </>
  );
}
