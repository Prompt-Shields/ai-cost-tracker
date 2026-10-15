"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import { ApiError } from "@/lib/api";
import type { PendingAccess as Pending } from "@/lib/api-connect";
import { CopyBlock } from "./CopyBlock";
import { AccessPill } from "./StatusPill";
import { usePoll, useConnectApi } from "./hooks";
import styles from "./connect.module.css";

const POLL_MS = 15_000;

/** What the subscription owner sees when a colleague sends them the share link. */
export function PendingAccess({ subscriptionId }: { subscriptionId: string }) {
  const { api } = useConnectApi();
  const [done, setDone] = useState(false);
  const load = useCallback(async () => {
    let p: Pending;
    try {
      p = await api.pending(subscriptionId);
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        throw new Error(
          "Your account isn't set up in AI Cost Tracker, so this page can't show the request. " +
            "Ask the colleague who sent the link for the role assignment command.",
        );
      }
      throw err;
    }
    if (p.status === "connected") setDone(true);
    return p;
  }, [api, subscriptionId]);
  // Poll until connected, then stop: there is nothing left to wait for.
  const { state, reload } = usePoll(load, done ? null : POLL_MS);

  return (
    <main className={`${styles.main} ${styles.narrow}`}>
      <div>
        <h1 className={styles.title}>Approve read-only access for AI cost tracking</h1>
        <p className={styles.lede}>
          A colleague wants to track Azure OpenAI and AI Foundry spend on this subscription in AI
          Cost Tracker. That needs one role assignment by someone with Owner or User Access
          Administrator rights on the subscription.
        </p>
      </div>
      {state.status === "loading" && (
        <div
          className={`${styles.panel} ${styles.skeleton}`}
          style={{ height: 260 }}
          aria-busy="true"
        />
      )}
      {state.status === "error" && (
        <div className={styles.notice} role="alert">
          <p className={styles.noticeTitle}>Couldn&apos;t load this request</p>
          <p className={styles.noticeBody}>{state.message}</p>
          <div className={styles.actions}>
            <button type="button" className={styles.secondary} onClick={reload}>
              Try again
            </button>
          </div>
        </div>
      )}
      {state.status === "ready" && <Body data={state.data} refreshError={state.refreshError} />}
    </main>
  );
}

function Body({ data, refreshError }: { data: Pending; refreshError: string | null }) {
  const connected = data.status === "connected";
  return (
    <>
      <section className={styles.panel}>
        <div className={styles.resultHead}>
          <span style={{ minWidth: 0 }}>
            <span className={styles.subName}>{data.name ?? "Subscription"}</span>
            <br />
            <span className={`${styles.mono} ${styles.muted}`}>{data.subscription_id}</span>
          </span>
          <AccessPill status={data.status} />
        </div>
        {connected ? (
          <p className={styles.sourceBody}>
            Access is in place and data is loading. Thank you, there is nothing else to do.{" "}
            <Link href="/connections" className={styles.link}>
              See connection health
            </Link>
          </p>
        ) : (
          <p className={styles.sourceBody} aria-live="polite">
            Waiting for the role assignment. This page checks every 15 seconds and updates by
            itself.
            {refreshError && ` (Last check failed: ${refreshError})`}
          </p>
        )}
      </section>

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>What is being asked</h2>
        <ul className={styles.steps}>
          <li>
            The built-in <strong>Reader</strong> role on this subscription, for the AI Cost
            Tracker&apos;s managed identity.
          </li>
          <li>
            Reader is read-only: it can see cost data, Azure Monitor token metrics and the list of
            AI deployments. It cannot change, create or delete anything, and it cannot read keys
            or data inside your resources.
          </li>
          <li>
            Why: so the app can show daily AI spend and tokens per model and deployment. No
            secrets are created and nothing expires; you can remove the role at any time.
          </li>
        </ul>
      </section>

      {!connected && (
        <section className={styles.panel}>
          <h2 className={styles.panelTitle}>How to grant it</h2>
          {data.command ? (
            <>
              <p className={styles.sourceBody}>
                Run this in Azure Cloud Shell or any terminal signed in with <code>az login</code>:
              </p>
              <CopyBlock text={data.command} label="Role assignment command" />
            </>
          ) : (
            <p className={styles.sourceBody}>
              Ask the colleague who sent this link for the role assignment command.
            </p>
          )}
          <p className={`${styles.muted} ${styles.small}`} style={{ marginTop: 10 }}>
            Prefer the portal? Subscription → Access control (IAM) → Add role assignment → Reader
            → Managed identity, and pick the AI Cost Tracker identity.
          </p>
        </section>
      )}
    </>
  );
}
