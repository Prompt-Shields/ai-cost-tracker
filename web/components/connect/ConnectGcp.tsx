"use client";

import Link from "next/link";
import { useCallback, useState, type FormEvent } from "react";
import {
  GCP_PROJECT_ID_RE,
  type GcpProjectResult,
  type GcpSetup,
  type GcpVisibleProject,
} from "@/lib/api-gcp";
import { CloudSwitcher } from "./CloudSwitcher";
import { CopyBlock, CopyButton } from "./CopyBlock";
import { AccessPill } from "./StatusPill";
import { errorMessage } from "./format";
import { useGcpApi, useIsAdmin, usePoll, type PollState } from "./hooks";
import styles from "./connect.module.css";

/** Visible projects change only when someone grants roles; a slow poll keeps the list honest. */
const VISIBLE_POLL_MS = 60_000;

export function ConnectGcp() {
  const gcp = useGcpApi();
  const isAdmin = useIsAdmin();
  const loadSetup = useCallback(() => gcp.setup(), [gcp]);
  const { state, reload } = usePoll(loadSetup, null);

  return (
    <main className={styles.main}>
      <CloudSwitcher active="gcp" />
      <div>
        <h1 className={styles.title}>Connect Google Cloud</h1>
        <p className={styles.lede}>
          Track Vertex AI spend and tokens per project. The app signs in to Google with its own
          Azure identity through workload identity federation: no service account keys, nothing to
          rotate, and only read-only roles.
        </p>
      </div>

      {state.status === "loading" && <Skeleton />}
      {state.status === "error" && (
        <div className={styles.notice} role="alert">
          <p className={styles.noticeTitle}>Couldn&apos;t load the Google Cloud setup</p>
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
          <p className={styles.noticeTitle}>Only admins can connect Google Cloud projects</p>
          <p className={styles.noticeBody}>
            Ask an admin of AI Cost Tracker to add your projects. You can see what is already
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

/** One-time setup: the script, the settings it produces, and a way to re-check afterwards. */
function SetupNeeded({ setup, onRecheck }: { setup: GcpSetup; onRecheck: () => void }) {
  return (
    <>
      <div className={styles.notice}>
        <p className={styles.noticeTitle}>One-time setup first</p>
        <p className={styles.noticeBody}>
          Someone who can create service accounts in a Google Cloud project runs one script, then
          an admin of this app sets the values it prints. After that, adding projects is just
          typing their ID here.
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
        <h2 className={styles.panelTitle}>1. Run the setup script</h2>
        <p className={styles.sourceBody}>
          Run it in{" "}
          <a
            href="https://shell.cloud.google.com/"
            target="_blank"
            rel="noreferrer"
            className={styles.link}
          >
            Cloud Shell
          </a>{" "}
          or any terminal signed in with <code>gcloud auth login</code>. It creates a workload
          identity pool that trusts only this app&apos;s Azure identity, and a service account with
          no keys. Safe to run again.
        </p>
        <CopyBlock text={setup.setup_script} label="One-time Google Cloud setup script" tall />
      </section>

      <section className={styles.panel}>
        <h2 className={styles.panelTitle}>2. Set the values and restart</h2>
        <p className={styles.sourceBody}>
          The script ends by printing the <code>ACT_GCP_*</code> settings. Add them to the API and
          worker configuration (the infra templates take them as parameters) and restart both. No
          secrets are involved.
        </p>
        <p className={styles.sourceBody}>
          The billing export table also needs{" "}
          <strong>detailed usage cost export to BigQuery</strong> turned on in your billing
          account. Step-by-step guide and troubleshooting:{" "}
          <code>{setup.docs_path}</code> in the repository.
        </p>
        <div className={styles.actions}>
          <button type="button" className={styles.primary} onClick={onRecheck}>
            Check again
          </button>
        </div>
      </section>

      <RolesPanel setup={setup} />
    </>
  );
}

function Configured({ setup }: { setup: GcpSetup }) {
  const gcp = useGcpApi();
  const loadVisible = useCallback(() => gcp.visibleProjects(), [gcp]);
  const visible = usePoll(loadVisible, VISIBLE_POLL_MS);
  // Most recent first; re-adding or re-checking a project replaces its card.
  const [results, setResults] = useState<GcpProjectResult[]>([]);

  const onResult = useCallback(
    (r: GcpProjectResult) => {
      setResults((prev) => [r, ...prev.filter((p) => p.project_id !== r.project_id)]);
      visible.update((rows) =>
        rows.map((p) =>
          p.project_id === r.project_id
            ? { ...p, tracked: true, access_status: r.access_status, name: r.name ?? p.name }
            : p,
        ),
      );
    },
    [visible.update],
  );

  return (
    <>
      <AddProject onResult={onResult} serviceAccount={setup.service_account} />
      {results.length > 0 && <Results results={results} onResult={onResult} />}
      <VisibleProjects state={visible.state} reload={visible.reload} onResult={onResult} />
      <BillingPanel setup={setup} />
      <RolesPanel setup={setup} />
    </>
  );
}

function AddProject({
  onResult,
  serviceAccount,
}: {
  onResult: (r: GcpProjectResult) => void;
  serviceAccount: string | null;
}) {
  const gcp = useGcpApi();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const id = value.trim().toLowerCase();
    if (!GCP_PROJECT_ID_RE.test(id)) {
      setError(
        "That doesn't look like a project ID. Use the ID (6–30 lowercase letters, digits or hyphens, starting with a letter), not the display name or number.",
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onResult(await gcp.addProject(id));
      setValue("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>Add a project</h2>
      <form className={styles.addForm} onSubmit={(e) => void submit(e)} noValidate>
        <div className={styles.field}>
          <label htmlFor="gcp-project-id" className={styles.small}>
            Project ID
          </label>
          <input
            id="gcp-project-id"
            type="text"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="e.g. my-ai-project-123"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            aria-describedby="gcp-project-hint"
            aria-invalid={error ? true : undefined}
          />
        </div>
        <button type="submit" className={styles.primary} disabled={busy || value.trim() === ""}>
          {busy ? "Checking access…" : "Add project"}
        </button>
      </form>
      <p id="gcp-project-hint" className={styles.hint}>
        Find it in the console&apos;s project picker. If the app can already read the project it
        connects straight away; otherwise you get two commands to send the project owner.
      </p>
      {serviceAccount && (
        <p className={styles.hint}>
          Service account: <span className={styles.mono}>{serviceAccount}</span>
        </p>
      )}
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
  results: GcpProjectResult[];
  onResult: (r: GcpProjectResult) => void;
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
          <li key={r.project_id} className={styles.result}>
            <ProjectResult result={r} onResult={onResult} />
          </li>
        ))}
      </ul>
    </section>
  );
}

/** One project's outcome: what happens next if connected, or what to send the owner if not. */
export function ProjectResult({
  result,
  onResult,
  showGrantLink = true,
}: {
  result: GcpProjectResult;
  onResult: (r: GcpProjectResult) => void;
  showGrantLink?: boolean;
}) {
  const connected = result.access_status === "connected";
  return (
    <>
      <div className={styles.resultHead}>
        <span style={{ minWidth: 0 }}>
          <span className={styles.subName}>{result.name ?? result.project_id}</span>
          <br />
          <span className={`${styles.mono} ${styles.muted}`}>{result.project_id}</span>
        </span>
        <AccessPill status={result.access_status} />
      </div>
      {connected ? (
        <p className={styles.sourceBody}>
          Deployments and the last 30 days of token usage load within minutes; cost history from
          the billing export loads in the background.{" "}
          <Link href="/connections" className={styles.link}>
            See progress on Connections
          </Link>
        </p>
      ) : (
        <>
          <p className={styles.sourceBody}>
            The app can&apos;t read this project yet. Someone with permission to change IAM on it
            (Owner or Project IAM Admin) runs these commands in Cloud Shell. It connects by itself
            within a few minutes after that, or press Check again.
          </p>
          {result.grant_commands && (
            <CopyBlock text={result.grant_commands} label={`Grant commands for ${result.project_id}`} />
          )}
          <div className={styles.actions} style={{ marginTop: 10 }}>
            <CheckAgain projectId={result.project_id} onResult={onResult} />
            {result.grant_commands && (
              <CopyButton text={result.grant_commands} label="Copy commands for the project owner" />
            )}
            {showGrantLink && (
              <Link
                href={`/connect/gcp/grant/${encodeURIComponent(result.project_id)}`}
                className={styles.link}
              >
                Open as a page
              </Link>
            )}
          </div>
        </>
      )}
    </>
  );
}

export function CheckAgain({
  projectId,
  onResult,
}: {
  projectId: string;
  onResult: (r: GcpProjectResult) => void;
}) {
  const gcp = useGcpApi();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const check = async () => {
    setBusy(true);
    setNote(null);
    try {
      const r = await gcp.check(projectId);
      if (r.access_status !== "connected") setNote("Still no access. Roles can take a minute or two to apply.");
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

const isAi = (p: GcpVisibleProject) => p.has_ai_resources;
const isTracked = (p: GcpVisibleProject) => p.tracked === true;

/** Untracked AI projects first (the ones to act on), then tracked, then by name. */
function sortProjects(rows: GcpVisibleProject[]): GcpVisibleProject[] {
  return [...rows].sort(
    (a, b) =>
      Number(isAi(b)) - Number(isAi(a)) ||
      Number(isTracked(a)) - Number(isTracked(b)) ||
      (a.name ?? a.project_id).localeCompare(b.name ?? b.project_id),
  );
}

function VisibleProjects({
  state,
  reload,
  onResult,
}: {
  state: PollState<GcpVisibleProject[]>;
  reload: () => void;
  onResult: (r: GcpProjectResult) => void;
}) {
  const [showAll, setShowAll] = useState(false);

  return (
    <section className={styles.panel}>
      <div className={styles.panelHead}>
        <h2 className={styles.panelTitle}>Projects the app can see</h2>
        {state.status === "ready" && (
          <button type="button" className={styles.textButton} onClick={reload}>
            Refresh
          </button>
        )}
      </div>
      {state.status === "loading" && (
        <div className={styles.skeleton} style={{ height: 120, borderRadius: 8 }} aria-busy="true">
          <span className={styles.srOnly}>Loading projects…</span>
        </div>
      )}
      {state.status === "error" && (
        <div role="alert">
          <p className={styles.inlineError} style={{ marginTop: 0 }}>
            Couldn&apos;t list projects: {state.message}
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
          None yet, which is normal: the service account only sees projects where it has a role.
          Add a project above and send its owner the commands; it shows up here once they run
          them.
        </p>
      )}
      {state.status === "ready" && state.data.length > 0 && (
        <ProjectList
          rows={sortProjects(state.data)}
          showAll={showAll}
          onShowAll={() => setShowAll(true)}
          onResult={onResult}
          refreshError={state.refreshError}
        />
      )}
    </section>
  );
}

function ProjectList({
  rows,
  showAll,
  onShowAll,
  onResult,
  refreshError,
}: {
  rows: GcpVisibleProject[];
  showAll: boolean;
  onShowAll: () => void;
  onResult: (r: GcpProjectResult) => void;
  refreshError: string | null;
}) {
  // Projects without Vertex AI are usually noise; hide them unless asked, or if they're all we have.
  const aiCount = rows.filter(isAi).length;
  const hidden = showAll || aiCount === 0 ? 0 : rows.length - aiCount;
  const shown = hidden ? rows.filter((p) => isAi(p) || isTracked(p)) : rows;
  return (
    <>
      <p className={`${styles.muted} ${styles.small}`}>
        {rows.length} project{rows.length === 1 ? "" : "s"} · {aiCount} with Vertex AI enabled
      </p>
      {refreshError && (
        <p className={styles.refreshNote} role="status">
          Couldn&apos;t refresh: {refreshError}. Showing the last loaded list.
        </p>
      )}
      <ul className={styles.resultList} style={{ marginTop: 8 }}>
        {shown.map((p) => (
          <li key={p.project_id} className={styles.projectRow}>
            <span style={{ minWidth: 0 }}>
              <span className={styles.subName}>{p.name ?? p.project_id}</span>
              <br />
              <span className={`${styles.mono} ${styles.muted}`}>{p.project_id}</span>
            </span>
            <span className={styles.projectMeta}>
              <span className={styles.badge}>
                {p.has_ai_resources ? "Vertex AI enabled" : "No Vertex AI"}
              </span>
              {isTracked(p) && p.access_status && <AccessPill status={p.access_status} />}
              {isTracked(p) ? (
                <span className={styles.badge}>Tracked</span>
              ) : (
                <TrackButton project={p} onResult={onResult} />
              )}
            </span>
          </li>
        ))}
      </ul>
      {hidden > 0 && shown.length < rows.length && (
        <div className={styles.actions} style={{ marginTop: 10 }}>
          <button type="button" className={styles.textButton} onClick={onShowAll}>
            Show {rows.length - shown.length} without Vertex AI
          </button>
        </div>
      )}
    </>
  );
}

function TrackButton({
  project,
  onResult,
}: {
  project: GcpVisibleProject;
  onResult: (r: GcpProjectResult) => void;
}) {
  const gcp = useGcpApi();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = project.name ?? project.project_id;
  const track = async () => {
    setBusy(true);
    setError(null);
    try {
      onResult(await gcp.addProject(project.project_id));
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

function BillingPanel({ setup }: { setup: GcpSetup }) {
  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>Billing export access</h2>
      {setup.billing_export_table ? (
        <p className={styles.sourceBody} style={{ marginTop: 0 }}>
          Cost comes from the BigQuery billing export{" "}
          <span className={styles.mono}>{setup.billing_export_table}</span>. Grant read access once
          (by someone who can change IAM on that dataset and its project); it covers every project
          billed to that billing account.
        </p>
      ) : (
        <p className={styles.sourceBody} style={{ marginTop: 0 }}>
          No billing export table is set (<code>ACT_GCP_BILLING_EXPORT_TABLE</code>), so cost
          isn&apos;t pulled; token usage and deployments still are. Turn on detailed usage cost
          export to BigQuery in your billing account, then set the table. See{" "}
          <code>{setup.docs_path}</code>.
        </p>
      )}
      {setup.billing_grant_commands && (
        <CopyBlock text={setup.billing_grant_commands} label="Billing export grant commands" />
      )}
    </section>
  );
}

/** What the service account is given and why, so owners can approve with confidence. */
function RolesPanel({ setup }: { setup: GcpSetup }) {
  if (setup.roles.length === 0) return null;
  return (
    <section className={styles.panel}>
      <h2 className={styles.panelTitle}>What the app gets</h2>
      <ul className={styles.roleList}>
        {setup.roles.map((r) => (
          <li key={`${r.role}:${r.scope}`}>
            <span style={{ minWidth: 0 }}>
              <span className={styles.mono}>{r.role}</span>
              <br />
              <span className={styles.roleScope}>on the {r.scope}</span>
            </span>
            <span className={styles.sourceBody} style={{ margin: 0 }}>
              {r.why}
            </span>
          </li>
        ))}
      </ul>
      <p className={`${styles.muted} ${styles.small}`} style={{ marginTop: 10 }}>
        All read-only. Remove the roles at any time to disconnect a project.
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
