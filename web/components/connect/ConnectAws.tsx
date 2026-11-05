"use client";

import Link from "next/link";
import { useCallback, useMemo, useState, type FormEvent } from "react";
import {
  AWS_ACCOUNT_ID_RE,
  AwsApi,
  normaliseAccountId,
  type AwsAccountResult,
  type AwsPermission,
  type AwsSetup,
  type AwsVisibleAccount,
} from "@/lib/api-aws";
import { useSession } from "../AuthProvider";
import { CloudSwitcher } from "./CloudSwitcher";
import { CopyBlock, CopyButton } from "./CopyBlock";
import { AccessPill } from "./StatusPill";
import { errorMessage } from "./format";
import { useIsAdmin, usePoll, type PollState } from "./hooks";
import styles from "./connect.module.css";

/** Visible accounts change only when someone deploys the stack; a slow poll keeps the list honest. */
const VISIBLE_POLL_MS = 60_000;

/** AWS onboarding client; no AWS sign-in needed, the API reaches AWS via web identity federation. */
export function useAwsApi(): AwsApi {
  const { api } = useSession();
  return useMemo(() => new AwsApi(api.getToken), [api]);
}

export function ConnectAws() {
  const aws = useAwsApi();
  const isAdmin = useIsAdmin();
  const loadSetup = useCallback(() => aws.setup(), [aws]);
  const { state, reload } = usePoll(loadSetup, null);

  return (
    <main className={styles.main}>
      <CloudSwitcher active="aws" />
      <div>
        <h1 className={styles.title}>Connect AWS</h1>
        <p className={styles.lede}>
          Track Amazon Bedrock spend and tokens per account. Each account gets a read-only IAM role
          from one CloudFormation stack, and the app assumes it with its own Azure identity: no
          access keys, nothing to rotate.
        </p>
      </div>

      {state.status === "loading" && <Skeleton />}
      {state.status === "error" && (
        <div className={styles.notice} role="alert">
          <p className={styles.noticeTitle}>Couldn&apos;t load the AWS setup</p>
          <p className={styles.noticeBody}>{state.message}</p>
          <div className={styles.actions}>
            <button type="button" className={styles.secondary} onClick={reload}>
              Try again
            </button>
          </div>
        </div>
      )}
      {state.status === "ready" && !state.data.configured && (
        <SetupNeeded setup={state.data} onRecheck={reload} />
      )}
      {state.status === "ready" && state.data.configured && isAdmin === null && <Skeleton />}
      {state.status === "ready" && state.data.configured && isAdmin === false && (
        <div className={styles.notice}>
          <p className={styles.noticeTitle}>Only admins can connect AWS accounts</p>
          <p className={styles.noticeBody}>
            Ask an admin of AI Cost Tracker to add your accounts. You can see what is already
            connected on the{" "}
            <Link href="/connections" className={styles.link}>
              Connections
            </Link>{" "}
            page.
          </p>
        </div>
      )}
      {state.status === "ready" && state.data.configured && isAdmin && (
        <Configured setup={state.data} />
      )}
    </main>
  );
}

/** One-time setup: the billing-account stack, the settings it needs, and a re-check. */
function SetupNeeded({ setup, onRecheck }: { setup: AwsSetup; onRecheck: () => void }) {
  return (
    <>
      <div className={styles.notice}>
        <p className={styles.noticeTitle}>One-time setup first</p>
        <p className={styles.noticeBody}>
          Someone with admin rights in your AWS billing (management) account deploys one
          CloudFormation stack, then an admin of this app sets a few values. After that, adding
          accounts is just typing their ID here.
        </p>
        {setup.missing.length > 0 && (
          <>
            <p className={`${styles.muted} ${styles.small}`} style={{ marginTop: 12 }}>
              Settings still missing:
            </p>
            <ul className={styles.missingList} aria-label="Missing settings">
              {setup.missing.map((name) => (
                <li key={name} className={`${styles.badge} ${styles.mono}`}>
                  {name}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>1. Deploy the stack in the billing account</h2>
        <p className={styles.sourceBody}>
          It creates the read-only role, the cost and usage data export (CUR 2.0) and the S3 bucket
          it lands in. Run it in{" "}
          <a
            href="https://console.aws.amazon.com/cloudshell/home"
            target="_blank"
            rel="noopener noreferrer"
            className={styles.link}
          >
            AWS CloudShell
          </a>{" "}
          signed in to the billing account (CloudShell needs the template file: download it and use
          Actions → Upload file), or upload the template in the CloudFormation console. Safe to run
          again.
        </p>
        <div className={styles.actions} style={{ marginTop: 10, marginBottom: 10 }}>
          <TemplateDownload />
        </div>
        <CopyBlock
          text={setup.deploy_command_export}
          label="Deploy command for the billing account"
        />
      </section>

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>2. Set the values and restart</h2>
        <p className={styles.sourceBody}>
          Add the missing <code>ACT_AWS_*</code> settings (the billing account ID and the export
          bucket the stack created) to the API and worker configuration and restart both. No
          secrets are involved.
        </p>
        <p className={styles.sourceBody}>
          The first cost data can take up to 24 hours to arrive after the export is created. Step
          by step guide and troubleshooting: <code>{setup.docs_path}</code> in the repository.
        </p>
        <div className={styles.actions}>
          <button type="button" className={styles.primary} onClick={onRecheck}>
            Check again
          </button>
        </div>
      </section>

      <PermissionsPanel permissions={setup.permissions} roleName={setup.role_name} />
    </>
  );
}

function Configured({ setup }: { setup: AwsSetup }) {
  const aws = useAwsApi();
  const loadVisible = useCallback(() => aws.visibleAccounts(), [aws]);
  const visible = usePoll(loadVisible, VISIBLE_POLL_MS);
  // Most recent first; re-adding or re-checking an account replaces its card.
  const [results, setResults] = useState<AwsAccountResult[]>([]);

  const onResult = useCallback(
    (r: AwsAccountResult) => {
      // A re-check may omit the setup links; keep the ones we already have.
      setResults((prev) => {
        const old = prev.find((p) => p.account_id === r.account_id);
        const merged = old
          ? {
              ...r,
              quick_create_url: r.quick_create_url ?? old.quick_create_url,
              deploy_command: r.deploy_command ?? old.deploy_command,
            }
          : r;
        return [merged, ...prev.filter((p) => p.account_id !== r.account_id)];
      });
      visible.update((rows) =>
        rows.map((a) =>
          a.account_id === r.account_id
            ? { ...a, tracked: true, access_status: r.access_status, name: r.name ?? a.name }
            : a,
        ),
      );
    },
    [visible.update],
  );

  return (
    <>
      <AddAccount onResult={onResult} roleName={setup.role_name} />
      {results.length > 0 && <Results results={results} onResult={onResult} />}
      <VisibleAccounts state={visible.state} reload={visible.reload} onResult={onResult} />
      <ExportPanel setup={setup} />
      <PermissionsPanel permissions={setup.permissions} roleName={setup.role_name} />
    </>
  );
}

function AddAccount({
  onResult,
  roleName,
}: {
  onResult: (r: AwsAccountResult) => void;
  roleName: string;
}) {
  const aws = useAwsApi();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const id = normaliseAccountId(value);
    if (!AWS_ACCOUNT_ID_RE.test(id)) {
      setError(
        "That doesn't look like an AWS account ID. Use the 12-digit number shown under your account name in the console's top-right menu, not the alias or email.",
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onResult(await aws.addAccount(id));
      setValue("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>Add an account</h2>
      <form className={styles.addForm} onSubmit={(e) => void submit(e)} noValidate>
        <div className={styles.field}>
          <label htmlFor="aws-account-id" className={styles.small}>
            Account ID
          </label>
          <input
            id="aws-account-id"
            type="text"
            inputMode="numeric"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="e.g. 123456789012"
            autoComplete="off"
            spellCheck={false}
            aria-describedby="aws-account-hint"
            aria-invalid={error ? true : undefined}
          />
        </div>
        <button type="submit" className={styles.primary} disabled={busy || value.trim() === ""}>
          {busy ? "Checking access…" : "Add account"}
        </button>
      </form>
      <p id="aws-account-hint" className={styles.hint}>
        If the account already has the read-only role it connects straight away; otherwise you get
        a one-click link to create it, to use yourself or send to the account owner.
      </p>
      <p className={styles.hint}>
        Role name: <span className={styles.mono}>{roleName}</span>
      </p>
      {error && (
        <p className={styles.inlineError} role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

function Results({
  results,
  onResult,
}: {
  results: AwsAccountResult[];
  onResult: (r: AwsAccountResult) => void;
}) {
  const waiting = results.filter((r) => r.access_status !== "connected").length;
  return (
    <section className={styles.panel} aria-live="polite">
      <h2 className={styles.panelTitle}>
        {results.length - waiting} connected
        {waiting > 0 && ` · ${waiting} waiting for access`}
      </h2>
      <ul className={styles.resultList}>
        {results.map((r) => (
          <li key={r.account_id} className={styles.result}>
            <AccountResult result={r} onResult={onResult} />
          </li>
        ))}
      </ul>
    </section>
  );
}

/** One account's outcome: what happens next if connected, or how to create the role if not. */
function AccountResult({
  result,
  onResult,
}: {
  result: AwsAccountResult;
  onResult: (r: AwsAccountResult) => void;
}) {
  const connected = result.access_status === "connected";
  return (
    <>
      <div className={styles.resultHead}>
        <span style={{ minWidth: 0 }}>
          <span className={styles.subName}>{result.name ?? result.account_id}</span>
          <br />
          <span className={`${styles.mono} ${styles.muted}`}>{result.account_id}</span>
        </span>
        <AccessPill status={result.access_status} />
      </div>
      {connected ? (
        <p className={styles.sourceBody}>
          Models and the last 30 days of token usage load within minutes; cost history from the
          data export loads in the background.{" "}
          <Link href="/connections" className={styles.link}>
            See progress on Connections
          </Link>
        </p>
      ) : (
        <>
          <p className={styles.sourceBody}>
            The app can&apos;t assume its role in this account yet. Someone who can create IAM roles
            there deploys the read-only stack once. It connects by itself within a few minutes
            after that, or press Check again.
          </p>
          <RoleSteps
            accountId={result.account_id}
            quickCreateUrl={result.quick_create_url}
            deployCommand={result.deploy_command}
          />
          <div className={styles.actions} style={{ marginTop: 10 }}>
            <CheckAgain accountId={result.account_id} onResult={onResult} />
            <Link
              href={`/connect/aws/account/${encodeURIComponent(result.account_id)}`}
              className={styles.link}
            >
              Open as a page to share
            </Link>
          </div>
        </>
      )}
    </>
  );
}

/**
 * The two ways to create the role in one account: the console quick-create link (preferred,
 * one click once signed in) and the CLI command with the template file.
 */
export function RoleSteps({
  accountId,
  quickCreateUrl,
  deployCommand,
}: {
  accountId: string;
  quickCreateUrl: string | null;
  deployCommand: string | null;
}) {
  return (
    <>
      {quickCreateUrl && (
        <>
          <div className={styles.actions} style={{ marginTop: 10 }}>
            <a
              href={quickCreateUrl}
              target="_blank"
              rel="noopener noreferrer"
              className={`${styles.primary} ${styles.linkButton}`}
            >
              Create the role in the AWS console
              <span className={styles.srOnly}> (opens in a new tab)</span>
            </a>
            <CopyButton text={quickCreateUrl} label="Copy link for the account owner" />
          </div>
          <p className={styles.hint}>
            Sign in to account <span className={styles.mono}>{accountId}</span> first, tick the IAM
            acknowledgement at the bottom of the page and press Create stack.
          </p>
        </>
      )}
      {deployCommand && (
        <>
          <p className={styles.sourceBody} style={{ marginTop: quickCreateUrl ? 14 : 10 }}>
            {quickCreateUrl ? "Or with the AWS CLI" : "With the AWS CLI"}, signed in to that
            account, next to the downloaded template:
          </p>
          <CopyBlock text={deployCommand} label={`Deploy command for ${accountId}`} />
          <div className={styles.actions} style={{ marginTop: 10 }}>
            <TemplateDownload />
          </div>
        </>
      )}
    </>
  );
}

/** Saves the CloudFormation template; it's behind sign-in, so fetch it with the token. */
export function TemplateDownload({ label = "Download template" }: { label?: string }) {
  const aws = useAwsApi();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      await aws.downloadTemplate();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button
        type="button"
        className={styles.secondary}
        disabled={busy}
        onClick={() => void download()}
      >
        {busy ? "Downloading…" : label}
      </button>
      {error && (
        <span className={styles.inlineError} role="alert" style={{ margin: 0 }}>
          Couldn&apos;t download the template: {error}
        </span>
      )}
    </>
  );
}

export function CheckAgain({
  accountId,
  onResult,
}: {
  accountId: string;
  onResult: (r: AwsAccountResult) => void;
}) {
  const aws = useAwsApi();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const check = async () => {
    setBusy(true);
    setNote(null);
    try {
      const r = await aws.check(accountId);
      if (r.access_status !== "connected")
        setNote("Still no access. Check the stack finished (CREATE_COMPLETE) in that account.");
      onResult(r);
    } catch (err) {
      setNote(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button type="button" className={styles.secondary} disabled={busy} onClick={() => void check()}>
        {busy ? "Checking…" : "Check again"}
      </button>
      {note && (
        <span className={`${styles.small} ${styles.muted}`} role="status">
          {note}
        </span>
      )}
    </>
  );
}

const isAi = (a: AwsVisibleAccount) => a.has_ai_resources;
const isTracked = (a: AwsVisibleAccount) => a.tracked === true;

/** Untracked Bedrock accounts first (the ones to act on), then tracked, then by name. */
function sortAccounts(rows: AwsVisibleAccount[]): AwsVisibleAccount[] {
  return [...rows].sort(
    (a, b) =>
      Number(isAi(b)) - Number(isAi(a)) ||
      Number(isTracked(a)) - Number(isTracked(b)) ||
      (a.name ?? a.account_id).localeCompare(b.name ?? b.account_id),
  );
}

function VisibleAccounts({
  state,
  reload,
  onResult,
}: {
  state: PollState<AwsVisibleAccount[]>;
  reload: () => void;
  onResult: (r: AwsAccountResult) => void;
}) {
  const [showAll, setShowAll] = useState(false);

  return (
    <section className={styles.panel}>
      <div className={styles.panelHead}>
        <h2 className={styles.panelTitle}>Accounts in your organization</h2>
        {state.status === "ready" && (
          <button type="button" className={styles.textButton} onClick={reload}>
            Refresh
          </button>
        )}
      </div>
      {state.status === "loading" && (
        <div className={styles.skeleton} style={{ height: 120, borderRadius: 8 }} aria-busy="true">
          <span className={styles.srOnly}>Loading accounts…</span>
        </div>
      )}
      {state.status === "error" && (
        <div role="alert">
          <p className={styles.inlineError} style={{ marginTop: 0 }}>
            Couldn&apos;t list accounts: {state.message}
          </p>
          <div className={styles.actions} style={{ marginTop: 10 }}>
            <button type="button" className={styles.secondary} onClick={reload}>
              Try again
            </button>
          </div>
        </div>
      )}
      {state.status === "ready" && state.data.length === 0 && (
        <p className={styles.sourceBody} style={{ marginTop: 0 }}>
          None found. The list comes from AWS Organizations through the billing account; if you
          don&apos;t use Organizations, add accounts by ID above.
        </p>
      )}
      {state.status === "ready" && state.data.length > 0 && (
        <AccountList
          rows={sortAccounts(state.data)}
          showAll={showAll}
          onShowAll={() => setShowAll(true)}
          onResult={onResult}
          refreshError={state.refreshError}
        />
      )}
    </section>
  );
}

function AccountList({
  rows,
  showAll,
  onShowAll,
  onResult,
  refreshError,
}: {
  rows: AwsVisibleAccount[];
  showAll: boolean;
  onShowAll: () => void;
  onResult: (r: AwsAccountResult) => void;
  refreshError: string | null;
}) {
  // Accounts without Bedrock usage are usually noise; hide them unless asked, or if they're all we have.
  const aiCount = rows.filter(isAi).length;
  const hidden = showAll || aiCount === 0 ? 0 : rows.length - aiCount;
  const shown = hidden ? rows.filter((a) => isAi(a) || isTracked(a)) : rows;
  return (
    <>
      <p className={`${styles.muted} ${styles.small}`}>
        {rows.length} account{rows.length === 1 ? "" : "s"} · {aiCount} using Bedrock
      </p>
      {refreshError && (
        <p className={styles.refreshNote} role="status">
          Couldn&apos;t refresh: {refreshError}. Showing the last loaded list.
        </p>
      )}
      <ul className={styles.resultList} style={{ marginTop: 8 }}>
        {shown.map((a) => (
          <li key={a.account_id} className={styles.projectRow}>
            <span style={{ minWidth: 0 }}>
              <span className={styles.subName}>{a.name ?? a.account_id}</span>
              <br />
              <span className={`${styles.mono} ${styles.muted}`}>{a.account_id}</span>
            </span>
            <span className={styles.projectMeta}>
              <span className={styles.badge}>
                {a.has_ai_resources ? "Bedrock in use" : "No Bedrock usage seen"}
              </span>
              {isTracked(a) && a.access_status && <AccessPill status={a.access_status} />}
              {isTracked(a) ? (
                <span className={styles.badge}>Tracked</span>
              ) : (
                <TrackButton account={a} onResult={onResult} />
              )}
            </span>
          </li>
        ))}
      </ul>
      {hidden > 0 && shown.length < rows.length && (
        <div className={styles.actions} style={{ marginTop: 10 }}>
          <button type="button" className={styles.textButton} onClick={onShowAll}>
            Show {rows.length - shown.length} without Bedrock usage
          </button>
        </div>
      )}
    </>
  );
}

function TrackButton({
  account,
  onResult,
}: {
  account: AwsVisibleAccount;
  onResult: (r: AwsAccountResult) => void;
}) {
  const aws = useAwsApi();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = account.name ?? account.account_id;
  const track = async () => {
    setBusy(true);
    setError(null);
    try {
      onResult(await aws.addAccount(account.account_id));
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };
  return (
    <>
      <button
        type="button"
        className={styles.secondary}
        disabled={busy}
        onClick={() => void track()}
        aria-label={`Track ${label}`}
      >
        {busy ? "Tracking…" : "Track"}
      </button>
      {error && (
        <span className={styles.inlineError} role="alert" style={{ margin: 0, flexBasis: "100%" }}>
          {error}
        </span>
      )}
    </>
  );
}

function ExportPanel({ setup }: { setup: AwsSetup }) {
  const exp = setup.export;
  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>Cost data export</h2>
      {exp ? (
        <p className={styles.sourceBody} style={{ marginTop: 0 }}>
          Cost comes from the data export <span className={styles.mono}>{exp.name}</span> in{" "}
          <span className={styles.mono} style={{ overflowWrap: "anywhere" }}>
            s3://{exp.bucket}/{exp.prefix}
          </span>{" "}
          (billing account <span className={styles.mono}>{exp.account_id}</span>, {exp.region}). It
          covers every account in the organization; nothing to set up per account. A new export
          can take up to 24 hours to deliver its first data.
        </p>
      ) : (
        <p className={styles.sourceBody} style={{ marginTop: 0 }}>
          No data export is set (<code>ACT_AWS_EXPORT_BUCKET</code>), so cost isn&apos;t pulled;
          token usage and models still are. Deploy the stack in the billing account with the
          command below, then set the values. See <code>{setup.docs_path}</code>.
        </p>
      )}
      {!exp && (
        <>
          <CopyBlock
            text={setup.deploy_command_export}
            label="Deploy command for the billing account"
          />
          <div className={styles.actions} style={{ marginTop: 10 }}>
            <TemplateDownload />
          </div>
        </>
      )}
    </section>
  );
}

/** What the role allows and why, so account owners can approve with confidence. */
export function PermissionsPanel({
  permissions,
  roleName,
  title = "What the app gets",
}: {
  permissions: AwsPermission[];
  roleName?: string;
  title?: string;
}) {
  if (permissions.length === 0) return null;
  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>{title}</h2>
      {roleName && (
        <p className={`${styles.muted} ${styles.small}`} style={{ marginTop: 0 }}>
          IAM role <span className={styles.mono}>{roleName}</span>, trusted only for this
          app&apos;s Azure identity.
        </p>
      )}
      <ul className={styles.roleList}>
        {permissions.map((p) => (
          <li key={p.permission}>
            <span className={styles.mono} style={{ overflowWrap: "anywhere" }}>
              {p.permission}
            </span>
            <span className={styles.sourceBody} style={{ margin: 0 }}>
              {p.why}
            </span>
          </li>
        ))}
      </ul>
      <p className={`${styles.muted} ${styles.small}`} style={{ marginTop: 10 }}>
        All read-only. Delete the stack at any time to disconnect an account.
      </p>
    </section>
  );
}

function Skeleton({ label = "Loading…" }: { label?: string }) {
  return (
    <div aria-busy="true" aria-live="polite">
      <p className={styles.muted}>{label}</p>
      <div className={`${styles.panel} ${styles.skeleton}`} style={{ height: 240, marginTop: 8 }} />
    </div>
  );
}
