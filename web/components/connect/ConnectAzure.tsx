"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ConnectResult, DiscoveredSubscription } from "@/lib/api-connect";
import { ArmConsentRequired } from "@/lib/armToken";
import { config } from "@/lib/config";
import { CopyBlock, CopyButton } from "./CopyBlock";
import { AccessPill } from "./StatusPill";
import { errorMessage } from "./format";
import { useConnectApi, useIsAdmin } from "./hooks";
import styles from "./connect.module.css";

/** Owners usually grant within minutes or hours; 15 s keeps the screen live without load. */
const PENDING_POLL_MS = 15_000;

type Phase =
  | { step: "discovering" }
  | { step: "consent" }
  | { step: "error"; message: string }
  | { step: "pick"; subs: DiscoveredSubscription[] }
  | { step: "results"; subs: DiscoveredSubscription[]; results: ConnectResult[] };

const isAi = (s: DiscoveredSubscription) => s.ai_accounts > 0;
const isConnected = (s: DiscoveredSubscription) =>
  s.tracked === true && s.access_status === "connected";

/** AI subscriptions first, then by name, so the pre-ticked ones are at the top. */
function sortSubs(subs: DiscoveredSubscription[]): DiscoveredSubscription[] {
  return [...subs].sort(
    (a, b) => Number(isAi(b)) - Number(isAi(a)) || a.name.localeCompare(b.name),
  );
}

export function ConnectAzure() {
  if (config.authDisabled) return <AuthDisabledNotice />;
  return <ConnectFlow />;
}

function ConnectFlow() {
  const { api, armToken } = useConnectApi();
  const isAdmin = useIsAdmin();
  const [phase, setPhase] = useState<Phase>({ step: "discovering" });

  const discover = useCallback(
    async (interactive: boolean) => {
      setPhase({ step: "discovering" });
      try {
        const token = await armToken(interactive);
        if (!token) return; // auth disabled; handled above
        const { subscriptions } = await api.discover(token);
        setPhase({ step: "pick", subs: sortSubs(subscriptions) });
      } catch (err) {
        if (err instanceof ArmConsentRequired) setPhase({ step: "consent" });
        else setPhase({ step: "error", message: errorMessage(err) });
      }
    },
    [api, armToken],
  );

  // Silent attempt on load; a popup needs a click, so consent falls back to a button.
  useEffect(() => {
    if (isAdmin) void discover(false);
  }, [isAdmin, discover]);

  return (
    <main className={styles.main}>
      <div>
        <h1 className={styles.title}>Connect Azure</h1>
        <p className={styles.lede}>
          Pick the subscriptions to track. The app gets the built-in <strong>Reader</strong> role
          on each one: enough to read cost, token usage and deployments, and nothing else. No
          secrets, nothing to create in Azure.
        </p>
      </div>

      {isAdmin === null && <Skeleton />}
      {isAdmin === false && (
        <div className={styles.notice}>
          <p className={styles.noticeTitle}>Only admins can connect Azure</p>
          <p className={styles.noticeBody}>
            Ask an admin of AI Cost Tracker to connect your subscriptions. You can see what is
            already connected on the <Link href="/connections" className={styles.link}>Connections</Link> page.
          </p>
        </div>
      )}
      {isAdmin && phase.step === "discovering" && <Skeleton label="Looking for your subscriptions…" />}
      {isAdmin && phase.step === "consent" && (
        <div className={styles.notice}>
          <p className={styles.noticeTitle}>Allow access to your subscription list</p>
          <p className={styles.noticeBody}>
            To find your subscriptions and AI resources, the app uses your own Azure sign-in, just
            for this step. Microsoft will ask you to confirm.
          </p>
          <div className={styles.actions}>
            <button type="button" className={styles.primary} onClick={() => void discover(true)}>
              Continue with Microsoft
            </button>
          </div>
        </div>
      )}
      {isAdmin && phase.step === "error" && (
        <div className={styles.notice} role="alert">
          <p className={styles.noticeTitle}>Couldn&apos;t list your subscriptions</p>
          <p className={styles.noticeBody}>{phase.message}</p>
          <div className={styles.actions}>
            <button type="button" className={styles.secondary} onClick={() => void discover(true)}>
              Try again
            </button>
          </div>
        </div>
      )}
      {isAdmin && phase.step === "pick" && (
        <Picker
          subs={phase.subs}
          onConnected={(results) => setPhase({ step: "results", subs: phase.subs, results })}
        />
      )}
      {isAdmin && phase.step === "results" && (
        <Results
          subs={phase.subs}
          results={phase.results}
          onBack={() => void discover(false)}
        />
      )}
    </main>
  );
}

function Picker({
  subs,
  onConnected,
}: {
  subs: DiscoveredSubscription[];
  onConnected: (results: ConnectResult[]) => void;
}) {
  const { api, armToken } = useConnectApi();
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(subs.filter((s) => isAi(s) && !isConnected(s)).map((s) => s.subscription_id)),
  );
  const [useMg, setUseMg] = useState(false);
  const [mgId, setMgId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (subs.length === 0) {
    return (
      <div className={styles.notice}>
        <p className={styles.noticeTitle}>No subscriptions found</p>
        <p className={styles.noticeBody}>
          Your Azure account can&apos;t see any subscriptions. Sign in with the account you use in
          the Azure portal, or ask someone with access to connect them.
        </p>
      </div>
    );
  }

  const aiCount = subs.filter(isAi).length;
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const mgMissing = useMg && mgId.trim() === "";
  const canConnect = selected.size > 0 && !mgMissing && !busy;

  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      const token = await armToken(true);
      if (!token) return;
      const { results } = await api.connect(token, {
        subscription_ids: [...selected],
        management_group_id: useMg ? mgId.trim() : null,
      });
      onConnected(results);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <>
      <section className={styles.panel}>
        <div className={styles.selectBar}>
          <h2 className={styles.panelTitle} style={{ margin: 0 }}>
            {subs.length} subscription{subs.length === 1 ? "" : "s"} · {aiCount} with AI resources
          </h2>
          <span className={styles.small}>
            <button
              type="button"
              className={styles.textButton}
              onClick={() => setSelected(new Set(subs.filter(isAi).map((s) => s.subscription_id)))}
            >
              Select AI ones
            </button>
            {" · "}
            <button type="button" className={styles.textButton} onClick={() => setSelected(new Set())}>
              Clear
            </button>
          </span>
        </div>
        <ul className={styles.subList}>
          {subs.map((s) => (
            <li key={s.subscription_id}>
              <label className={styles.subRow}>
                <input
                  type="checkbox"
                  checked={selected.has(s.subscription_id)}
                  onChange={() => toggle(s.subscription_id)}
                />
                <span style={{ minWidth: 0 }}>
                  <span className={styles.subName}>{s.name}</span>
                  <br />
                  <span className={`${styles.mono} ${styles.muted}`}>{s.subscription_id}</span>
                </span>
                <span className={styles.subMeta}>
                  {isAi(s) ? (
                    <span className={styles.badge}>
                      {s.ai_accounts} AI account{s.ai_accounts === 1 ? "" : "s"} ·{" "}
                      {s.ai_deployments} deployment{s.ai_deployments === 1 ? "" : "s"}
                    </span>
                  ) : (
                    <span className={styles.badge}>No AI resources</span>
                  )}
                  {s.access_status && s.tracked && <AccessPill status={s.access_status} />}
                </span>
              </label>
            </li>
          ))}
        </ul>
      </section>

      <section className={styles.panel}>
        <label className={styles.check}>
          <input type="checkbox" checked={useMg} onChange={(e) => setUseMg(e.target.checked)} />
          Grant once on a management group instead
        </label>
        <p className={`${styles.muted} ${styles.small}`} style={{ marginTop: 4 }}>
          Reader on a management group covers every subscription under it, including ones created
          later. You need Owner or User Access Administrator on the group.
        </p>
        {useMg && (
          <div className={styles.field}>
            <label htmlFor="mg-id" className={styles.small}>
              Management group ID
            </label>
            <input
              id="mg-id"
              type="text"
              value={mgId}
              onChange={(e) => setMgId(e.target.value)}
              placeholder="e.g. contoso-platform"
              autoComplete="off"
              spellCheck={false}
            />
          </div>
        )}
        <div className={styles.actions}>
          <button type="button" className={styles.primary} disabled={!canConnect} onClick={() => void connect()}>
            {busy
              ? "Connecting…"
              : `Connect ${selected.size} subscription${selected.size === 1 ? "" : "s"}`}
          </button>
          {selected.size === 0 && <span className={styles.muted}>Tick at least one subscription.</span>}
        </div>
        {error && (
          <p className={styles.inlineError} role="alert">
            {error}
          </p>
        )}
      </section>
    </>
  );
}

function Results({
  subs,
  results: initial,
  onBack,
}: {
  subs: DiscoveredSubscription[];
  results: ConnectResult[];
  onBack: () => void;
}) {
  const { api } = useConnectApi();
  const [results, setResults] = useState(initial);
  const names = useMemo(() => new Map(subs.map((s) => [s.subscription_id, s.name])), [subs]);
  const waitingIds = results.filter((r) => r.status === "waiting_for_access").map((r) => r.subscription_id);
  const waitingKey = waitingIds.join(",");

  // Subscriptions connect themselves once the owner grants the role; reflect that live.
  useEffect(() => {
    if (!waitingKey) return;
    const ids = waitingKey.split(",");
    const timer = setInterval(() => {
      if (document.hidden) return;
      for (const id of ids) {
        api
          .pending(id)
          .then((p) => {
            if (p.status !== "connected") return;
            setResults((prev) =>
              prev.map((r) => (r.subscription_id === id ? { ...r, status: "connected" } : r)),
            );
          })
          .catch(() => {}); // Transient; the next tick retries.
      }
    }, PENDING_POLL_MS);
    return () => clearInterval(timer);
  }, [api, waitingKey]);

  const connected = results.length - waitingIds.length;
  return (
    <section className={styles.panel}>
      <div className={styles.panelHead}>
        <h2 className={styles.panelTitle}>
          {connected} connected
          {waitingIds.length > 0 && ` · ${waitingIds.length} waiting for access`}
        </h2>
        <button type="button" className={styles.secondary} onClick={onBack}>
          Connect more
        </button>
      </div>
      <ul className={styles.resultList}>
        {results.map((r) => (
          <li key={r.subscription_id} className={styles.result}>
            <div className={styles.resultHead}>
              <span style={{ minWidth: 0 }}>
                <span className={styles.subName}>{names.get(r.subscription_id) ?? r.subscription_id}</span>
                <br />
                <span className={`${styles.mono} ${styles.muted}`}>{r.subscription_id}</span>
              </span>
              <AccessPill status={r.status} />
            </div>
            {r.status === "connected" ? (
              <p className={styles.sourceBody}>
                Deployments show up in seconds and the last 30 days of token usage within a minute.
                12 months of cost history loads in the background.{" "}
                <Link href="/connections" className={styles.link}>
                  See progress on Connections
                </Link>
              </p>
            ) : (
              <WaitingHelp result={r} />
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function WaitingHelp({ result }: { result: ConnectResult }) {
  return (
    <div>
      <p className={styles.sourceBody}>
        You can&apos;t assign roles on this subscription (that needs Owner or User Access
        Administrator). Send the owner the link below, or ask them to run this command. It
        connects by itself as soon as the role exists; nobody needs to come back here.
      </p>
      {result.command && <CopyBlock text={result.command} label="Role assignment command" />}
      {result.share_url && (
        <div className={styles.actions} style={{ marginTop: 10 }}>
          <CopyButton text={result.share_url} label="Copy link for the subscription owner" />
        </div>
      )}
    </div>
  );
}

function AuthDisabledNotice() {
  return (
    <main className={styles.main}>
      <h1 className={styles.title}>Connect Azure</h1>
      <div className={styles.notice}>
        <p className={styles.noticeTitle}>Sign-in is turned off</p>
        <p className={styles.noticeBody}>
          Connecting Azure uses your own Azure sign-in to list subscriptions and grant the Reader
          role, so it needs Entra ID. This build has NEXT_PUBLIC_AUTH_DISABLED=true (local
          development). Configure the Entra settings to use this page, or run{" "}
          <code>act seed-demo</code> for demo data.
        </p>
      </div>
    </main>
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
