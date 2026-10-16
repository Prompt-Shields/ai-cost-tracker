"use client";

import { useMemo, useState } from "react";
import { API_BASE_URL, type Role } from "@/lib/api";
import { AdminApi, isAdmin, type ApiKey, type CreatedApiKey, type KeyRole } from "@/lib/api-m2-admin";
import { useSession } from "../AuthProvider";
import { CopyBlock } from "../connect/CopyBlock";
import { EmptyNotice, ErrorNotice, Skeleton, errorMessage, ui, useLoad } from "../ui";
import styles from "./admin.module.css";
import { formatWhen } from "./format";
import { useMe } from "./useMe";

const ROLE_INFO: { role: KeyRole; label: string; hint: string }[] = [
  { role: "viewer", label: "Viewer", hint: "read cost and usage" },
  { role: "analyst", label: "Analyst", hint: "read, plus exports" },
  { role: "admin", label: "Admin", hint: "change settings" },
];

/** Programmatic access (#35): keys are shown once, then only their prefix. */
export function ApiKeys() {
  const { api } = useSession();
  const admin = useMemo(() => new AdminApi(api.getToken), [api]);
  const { me } = useMe();
  const [version, setVersion] = useState(0);
  const [keys, retry] = useLoad(() => admin.apiKeys(), `keys|${version}`);
  const [created, setCreated] = useState<CreatedApiKey | null>(null);
  const canManage = isAdmin(me?.roles);
  const reload = () => setVersion((v) => v + 1);

  return (
    <main className={ui.main}>
      <div className={ui.toolbar}>
        <div>
          <h1 className={ui.title}>API keys</h1>
          <p className={ui.subtitle}>For scripts and BI tools that read cost data without signing in.</p>
        </div>
      </div>

      {created && <NewKey created={created} onDone={() => setCreated(null)} />}

      {canManage && (
        <CreateKey
          admin={admin}
          roles={me?.roles ?? []}
          onCreated={(k) => {
            setCreated(k);
            reload();
          }}
        />
      )}

      <section className={ui.panel}>
        <h2 className={ui.panelTitle}>Keys</h2>
        {keys.status === "loading" && <Skeleton height={160} label="Loading API keys…" />}
        {keys.status === "error" && (
          <ErrorNotice title="Couldn't load API keys" message={keys.message} onRetry={retry} />
        )}
        {keys.status === "ready" && keys.data.length === 0 && (
          <EmptyNotice title="No API keys yet">
            {canManage
              ? "Create one above to call the API from a script."
              : "Ask an admin to create one for you."}
          </EmptyNotice>
        )}
        {keys.status === "ready" && keys.data.length > 0 && (
          <KeyTable keys={keys.data} admin={admin} canManage={canManage} onChanged={reload} />
        )}
      </section>

      <section className={ui.panel}>
        <h2 className={ui.panelTitle}>Using a key</h2>
        <p className={ui.muted} style={{ fontSize: 14 }}>
          Send it as a bearer token. The key acts with the roles it was given, never more.
        </p>
        <CopyBlock
          label="Example request"
          text={`curl -H "Authorization: Bearer act_…" ${API_BASE_URL}/api/v1/summary`}
        />
      </section>
    </main>
  );
}

function CreateKey({
  admin,
  roles,
  onCreated,
}: {
  admin: AdminApi;
  roles: Role[];
  onCreated: (k: CreatedApiKey) => void;
}) {
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<KeyRole[]>(["viewer"]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Only owners may mint keys that can change settings.
  const offered = ROLE_INFO.filter((r) => r.role !== "admin" || roles.includes("owner"));

  const toggle = (role: KeyRole) =>
    setPicked((p) => (p.includes(role) ? p.filter((r) => r !== role) : [...p, role]));

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const k = await admin.createApiKey({ name: name.trim(), roles: picked });
      setName("");
      setPicked(["viewer"]);
      onCreated(k);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={ui.panel}>
      <h2 className={ui.panelTitle}>Create a key</h2>
      <form
        className={styles.form}
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <label className={styles.field} style={{ flex: "1 1 220px", maxWidth: 360 }}>
          <span className={styles.fieldLabel}>Name</span>
          <input
            className={styles.text}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Power BI refresh"
            maxLength={200}
            required
            autoComplete="off"
          />
        </label>
        <fieldset className={styles.checks}>
          <legend>Roles</legend>
          {offered.map((r) => (
            <label key={r.role} className={styles.check} title={r.hint}>
              <input
                type="checkbox"
                checked={picked.includes(r.role)}
                onChange={() => toggle(r.role)}
              />
              {r.label}
            </label>
          ))}
        </fieldset>
        <button
          type="submit"
          className={styles.primary}
          disabled={busy || !name.trim() || picked.length === 0}
        >
          {busy ? "Creating…" : "Create key"}
        </button>
      </form>
      {!roles.includes("owner") && (
        <p className={styles.hint}>Only owners can create keys with the admin role.</p>
      )}
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

function NewKey({ created, onDone }: { created: CreatedApiKey; onDone: () => void }) {
  return (
    <section className={styles.reveal} aria-live="polite">
      <p className={styles.revealTitle}>Copy “{created.name}” now</p>
      <p className={styles.revealBody}>
        This is the only time the full key is shown. Store it in a secret manager; if it&apos;s
        lost, revoke it and create a new one.
      </p>
      <CopyBlock label="New API key" text={created.key} />
      <div className={styles.actions}>
        <button type="button" className={styles.secondary} onClick={onDone}>
          I&apos;ve stored it
        </button>
      </div>
    </section>
  );
}

function KeyTable({
  keys,
  admin,
  canManage,
  onChanged,
}: {
  keys: ApiKey[];
  admin: AdminApi;
  canManage: boolean;
  onChanged: () => void;
}) {
  const sorted = [...keys].sort(
    (a, b) =>
      Number(!!a.revoked_at) - Number(!!b.revoked_at) || b.created_at.localeCompare(a.created_at),
  );
  return (
    <div className={`${ui.tableWrap} ${styles.clip}`}>
      <table className={ui.table}>
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col">Key</th>
            <th scope="col">Roles</th>
            <th scope="col">Created</th>
            <th scope="col">Last used</th>
            <th scope="col">Status</th>
            {canManage && (
              <th scope="col">
                <span className={ui.srOnly}>Actions</span>
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {sorted.map((k) => (
            <KeyRow key={k.id} k={k} admin={admin} canManage={canManage} onChanged={onChanged} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function KeyRow({
  k,
  admin,
  canManage,
  onChanged,
}: {
  k: ApiKey;
  admin: AdminApi;
  canManage: boolean;
  onChanged: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const revoked = !!k.revoked_at;

  const revoke = async () => {
    setBusy(true);
    setError(null);
    try {
      await admin.revokeApiKey(k.id);
      setConfirming(false);
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr className={revoked ? styles.dimmed : undefined}>
      <th scope="row">{k.name}</th>
      <td className={styles.mono}>{k.prefix}…</td>
      <td>
        <div className={ui.chips}>
          {k.roles.map((r) => (
            <span key={r} className={ui.chip}>
              {r}
            </span>
          ))}
        </div>
      </td>
      <td>
        {formatWhen(k.created_at)}
        {k.created_by && <div className={ui.cellSub}>by {k.created_by}</div>}
      </td>
      <td>{k.last_used_at ? formatWhen(k.last_used_at) : "Never"}</td>
      <td>
        {revoked ? (
          <span className={styles.pillMuted} title={formatWhen(k.revoked_at)}>
            Revoked
          </span>
        ) : (
          <span className={styles.pillOpen}>Active</span>
        )}
      </td>
      {canManage && (
        <td>
          {!revoked && !confirming && (
            <button
              type="button"
              className={`${styles.secondary} ${styles.small}`}
              onClick={() => setConfirming(true)}
            >
              Revoke
            </button>
          )}
          {!revoked && confirming && (
            <span className={ui.controls} role="group" aria-label={`Confirm revoking ${k.name}`}>
              <span style={{ fontSize: 13 }}>Scripts using it stop working.</span>
              <button
                type="button"
                className={`${styles.danger} ${styles.small}`}
                onClick={() => void revoke()}
                disabled={busy}
              >
                {busy ? "Revoking…" : "Revoke key"}
              </button>
              <button
                type="button"
                className={`${styles.secondary} ${styles.small}`}
                onClick={() => setConfirming(false)}
                disabled={busy}
              >
                Cancel
              </button>
            </span>
          )}
          {error && (
            <div className={styles.error} role="alert">
              {error}
            </div>
          )}
        </td>
      )}
    </tr>
  );
}
