"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import type { AwsAccountResult, AwsAccountSetup as Setup, AwsPermission } from "@/lib/api-aws";
import { CheckAgain, PermissionsPanel, RoleSteps, useAwsApi } from "./ConnectAws";
import { AccessPill } from "./StatusPill";
import { useIsAdmin, usePoll } from "./hooks";
import styles from "./connect.module.css";

interface Data {
  setup: Setup;
  permissions: AwsPermission[];
}

/** Export-account-only permissions don't apply to a member account; leave them out here. */
const EXPORT_ONLY = /^(organizations|s3):/i;

/** How to create the role in one waiting account, on its own page so it's easy to share. */
export function AwsAccountSetup({ accountId }: { accountId: string }) {
  const aws = useAwsApi();
  const isAdmin = useIsAdmin() === true;
  const load = useCallback(async (): Promise<Data> => {
    const [setup, general] = await Promise.all([
      aws.accountSetup(accountId),
      aws.setup().catch(() => null), // Permissions are context only; the steps matter.
    ]);
    return {
      setup,
      permissions: (general?.permissions ?? []).filter((p) => !EXPORT_ONLY.test(p.permission)),
    };
  }, [aws, accountId]);
  const { state, reload } = usePoll(load, null);

  return (
    <main className={`${styles.main} ${styles.narrow}`}>
      <div>
        <p className={styles.small}>
          <Link href="/connect/aws" className={styles.link}>
            ← Connect AWS
          </Link>
        </p>
        <h1 className={styles.title}>Create the read-only role in an AWS account</h1>
        <p className={styles.lede}>
          AI Cost Tracker needs a read-only IAM role in this account to show Amazon Bedrock tokens
          and models. Someone who can create IAM roles in the account deploys one CloudFormation
          stack, once. It contains no keys or secrets.
        </p>
      </div>
      {state.status === "loading" && (
        <div className={`${styles.panel} ${styles.skeleton}`} style={{ height: 260 }} aria-busy="true">
          <span className={styles.srOnly}>Loading…</span>
        </div>
      )}
      {state.status === "error" && (
        <div className={styles.notice} role="alert">
          <p className={styles.noticeTitle}>Couldn&apos;t load the setup for this account</p>
          <p className={styles.noticeBody}>{state.message}</p>
          <div className={styles.actions}>
            <button type="button" className={styles.secondary} onClick={reload}>
              Try again
            </button>
          </div>
        </div>
      )}
      {state.status === "ready" && <Body data={state.data} isAdmin={isAdmin} />}
    </main>
  );
}

function Body({ data: { setup, permissions }, isAdmin }: { data: Data; isAdmin: boolean }) {
  const [result, setResult] = useState<AwsAccountResult | null>(null);
  const connected = result?.access_status === "connected";
  return (
    <>
      <section className={styles.panel} aria-live="polite">
        <div className={styles.resultHead}>
          <span style={{ minWidth: 0 }}>
            <span className={styles.subName}>{result?.name ?? setup.account_id}</span>
            <br />
            <span className={`${styles.mono} ${styles.muted}`}>{setup.account_id}</span>
          </span>
          {result && <AccessPill status={result.access_status} />}
        </div>
        {connected ? (
          <p className={styles.sourceBody}>
            The role is in place and data is loading. Nothing else to do.{" "}
            <Link href="/connections" className={styles.link}>
              See connection health
            </Link>
          </p>
        ) : (
          <p className={styles.sourceBody}>
            The account connects by itself within a few minutes after the stack is created.
            {isAdmin && " Check now if you can't wait."}
          </p>
        )}
        {!connected && isAdmin && (
          <div className={styles.actions} style={{ marginTop: 10 }}>
            <CheckAgain accountId={setup.account_id} onResult={setResult} />
          </div>
        )}
      </section>

      {!connected && (
        <section className={styles.panel}>
          <h2 className={styles.panelTitle}>Steps for the account owner</h2>
          <p className={styles.sourceBody} style={{ marginTop: 0 }}>
            Send this page or the link below as it is; it contains no secrets.
          </p>
          <RoleSteps
            accountId={setup.account_id}
            quickCreateUrl={setup.quick_create_url}
            deployCommand={setup.deploy_command}
          />
          <p className={`${styles.muted} ${styles.small}`} style={{ marginTop: 12 }}>
            It creates role{" "}
            <span className={styles.mono} style={{ overflowWrap: "anywhere" }}>
              {setup.role_arn}
            </span>
            , trusted only for this app&apos;s Azure identity.
          </p>
        </section>
      )}

      <PermissionsPanel permissions={permissions} title="What is being asked" />
    </>
  );
}
