"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  alertKindLabel,
  type ChannelInput,
  type CreatedChannel,
  type Delivery,
  type NotificationChannel,
} from "@/lib/api-m2-alerts";
import { CopyBlock } from "../connect/CopyBlock";
import { errorMessage } from "../connect/format";
import { useIsAdmin, usePoll, type PollState } from "../connect/hooks";
import connect from "../connect/connect.module.css";
import { ui } from "../ui";
import { Pill, When, m2, severityLabel, useAlertsApi } from "../alerts/shared";
import { ChannelForm } from "./ChannelForm";
import { KindIcon, kindLabel } from "./kinds";

type TestState = { status: "running" } | { status: "ok"; text: string } | { status: "error"; text: string };

export function Notifications() {
  const api = useAlertsApi();
  const isAdmin = useIsAdmin() === true;
  const loadChannels = useCallback(
    () => api.channels().then((rows) => [...rows].sort((a, b) => a.name.localeCompare(b.name))),
    [api],
  );
  const channels = usePoll(loadChannels, null);
  const loadDeliveries = useCallback(() => api.deliveries(50), [api]);
  const deliveries = usePoll(loadDeliveries, 60_000);

  const [editing, setEditing] = useState<NotificationChannel | "new" | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [secret, setSecret] = useState<{ name: string; value: string } | null>(null);
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [rowError, setRowError] = useState<string | null>(null);
  const formRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (editing) formRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [editing]);

  const save = async (input: ChannelInput) => {
    if (editing && editing !== "new") {
      await api.updateChannel(editing.id, input);
    } else {
      const created: CreatedChannel = await api.createChannel(input);
      if (created?.signing_secret) setSecret({ name: created.name, value: created.signing_secret });
    }
    setNote(`Saved “${input.name}”.`);
    setEditing(null);
    channels.reload();
  };

  const remove = async (c: NotificationChannel) => {
    if (!window.confirm(`Delete the channel “${c.name}”? Alerts stop going there.`)) return;
    setRowError(null);
    try {
      await api.deleteChannel(c.id);
      setNote(`Deleted “${c.name}”.`);
      if (editing !== "new" && editing?.id === c.id) setEditing(null);
      channels.reload();
    } catch (err) {
      setRowError(`Couldn't delete “${c.name}”: ${errorMessage(err)}`);
    }
  };

  const test = async (c: NotificationChannel) => {
    setTests((t) => ({ ...t, [c.id]: { status: "running" } }));
    let next: TestState;
    try {
      const res = await api.testChannel(c.id);
      const failed = res?.ok === false || res?.status === "failed" || !!res?.error;
      next = failed
        ? { status: "error", text: `Test failed: ${res?.error ?? res?.detail ?? "the channel rejected it"}` }
        : { status: "ok", text: `Test message sent to ${kindLabel(c.kind)}. Check that it arrived.` };
    } catch (err) {
      next = { status: "error", text: `Test failed: ${errorMessage(err)}` };
    }
    setTests((t) => ({ ...t, [c.id]: next }));
    deliveries.reload();
  };

  const { state } = channels;
  const channelNames = new Map(state.status === "ready" ? state.data.map((c) => [c.id, c.name]) : []);

  return (
    <main className={ui.main}>
      <div className={ui.toolbar}>
        <div>
          <h1 className={ui.title}>Notifications</h1>
          <p className={m2.lede}>
            Where alerts go: Slack, Microsoft Teams, email or your own webhook. Each channel can
            take every alert or only some types and severities, as they happen or as a daily digest.
          </p>
        </div>
        {isAdmin && editing === null && !(state.status === "ready" && state.data.length === 0) && (
          <button
            type="button"
            className={connect.primary}
            onClick={() => {
              setNote(null);
              setEditing("new");
            }}
          >
            New channel
          </button>
        )}
      </div>

      {secret && (
        <section className={m2.secret} role="status" aria-label="Signing secret">
          <p className={ui.noticeTitle} style={{ margin: 0 }}>
            Signing secret for “{secret.name}”
          </p>
          <p className={m2.help} style={{ fontSize: 13 }}>
            Copy it now: it won&apos;t be shown again. Your endpoint uses it to check the HMAC-SHA256
            signature on each request.
          </p>
          <CopyBlock text={secret.value} label="Signing secret" />
          <div className={connect.actions} style={{ marginTop: 12 }}>
            <button type="button" className={connect.secondary} onClick={() => setSecret(null)}>
              I&apos;ve copied it
            </button>
          </div>
        </section>
      )}

      {note && (
        <p className={connect.refreshNote} role="status">
          {note}
        </p>
      )}

      {isAdmin && editing !== null && (
        <div ref={formRef} style={{ scrollMarginTop: 16 }}>
          <ChannelForm
            channel={editing === "new" ? null : editing}
            onSave={save}
            onCancel={() => setEditing(null)}
          />
        </div>
      )}

      {state.status === "loading" && (
        <div className={`${ui.panel} ${ui.skeleton}`} style={{ height: 200 }} aria-busy="true">
          <span className={ui.srOnly}>Loading channels…</span>
        </div>
      )}
      {state.status === "error" && (
        <div className={ui.notice} role="alert">
          <p className={ui.noticeTitle}>Couldn&apos;t load notification channels</p>
          <p className={ui.noticeBody}>{state.message}</p>
          <button type="button" className={ui.retry} onClick={channels.reload}>
            Try again
          </button>
        </div>
      )}
      {state.status === "ready" && state.data.length === 0 && editing === null && (
        <div className={ui.notice}>
          <p className={ui.noticeTitle}>No channels yet</p>
          <p className={ui.noticeBody}>
            {isAdmin
              ? "Add a Slack, Teams, email or webhook channel so budget and spend alerts reach people."
              : "Alerts only show in the inbox until an admin adds a channel."}
          </p>
          {isAdmin && (
            <div className={connect.actions} style={{ justifyContent: "center" }}>
              <button type="button" className={connect.primary} onClick={() => setEditing("new")}>
                New channel
              </button>
            </div>
          )}
        </div>
      )}
      {state.status === "ready" && state.data.length > 0 && (
        <section className={ui.panel} aria-labelledby="channels-title">
          <h2 className={ui.panelTitle} id="channels-title">
            Channels
          </h2>
          {rowError && (
            <p className={connect.inlineError} role="alert" style={{ marginTop: 0, marginBottom: 8 }}>
              {rowError}
            </p>
          )}
          <ul className={m2.list}>
            {state.data.map((c) => (
              <ChannelItem
                key={c.id}
                channel={c}
                isAdmin={isAdmin}
                test={tests[c.id]}
                onTest={() => void test(c)}
                onEdit={() => {
                  setNote(null);
                  setEditing(c);
                }}
                onDelete={() => void remove(c)}
              />
            ))}
          </ul>
        </section>
      )}

      <DeliveriesPanel state={deliveries.state} names={channelNames} onRetry={deliveries.reload} />
    </main>
  );
}

function ChannelItem({
  channel: c,
  isAdmin,
  test,
  onTest,
  onEdit,
  onDelete,
}: {
  channel: NotificationChannel;
  isAdmin: boolean;
  test: TestState | undefined;
  onTest: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const kinds = c.alert_kinds.length === 0 ? "All alert types" : c.alert_kinds.map(alertKindLabel).join(", ");
  return (
    <li className={m2.item}>
      <div className={m2.itemHead}>
        <div className={m2.itemTitle}>
          <KindIcon kind={c.kind} />
          <div style={{ minWidth: 0 }}>
            <div className={m2.itemName}>{c.name}</div>
            <div className={m2.dim} style={{ fontSize: 13 }}>
              {kindLabel(c.kind)}
            </div>
            <div className={`${m2.mono} ${m2.dim}`}>{c.target_masked}</div>
          </div>
        </div>
        <Pill tone={c.active ? "ok" : "off"} label={c.active ? "Active" : "Paused"} />
      </div>
      <div className={m2.meta}>
        <span>{kinds}</span>
        <span>{severityLabel(c.min_severity)} and above</span>
        <span>{c.digest ? "Daily digest" : "As they happen"}</span>
      </div>
      {isAdmin && (
        <div className={connect.actions} style={{ marginTop: 10 }}>
          <button
            type="button"
            className={connect.secondary}
            onClick={onTest}
            disabled={test?.status === "running"}
          >
            {test?.status === "running" ? "Sending…" : "Send test"}
          </button>
          <button type="button" className={m2.iconButton} onClick={onEdit}>
            Edit
          </button>
          <button type="button" className={`${m2.iconButton} ${m2.danger}`} onClick={onDelete}>
            Delete
          </button>
        </div>
      )}
      {test && test.status !== "running" && (
        <p
          className={`${m2.result} ${test.status === "ok" ? m2.resultOk : m2.resultErr}`}
          role={test.status === "error" ? "alert" : "status"}
        >
          {test.text}
        </p>
      )}
    </li>
  );
}

function DeliveriesPanel({
  state,
  names,
  onRetry,
}: {
  state: PollState<Delivery[]>;
  names: Map<string, string>;
  onRetry: () => void;
}) {
  return (
    <section className={ui.panel} aria-labelledby="deliveries-title">
      <div className={ui.panelHead}>
        <h2 className={ui.panelTitle} id="deliveries-title">
          Recent deliveries
        </h2>
        <span className={m2.dim} style={{ fontSize: 13 }}>
          Last 50 · refreshes every minute
        </span>
      </div>
      {state.status === "loading" && (
        <div className={ui.skeleton} style={{ height: 120, borderRadius: 8 }} aria-busy="true">
          <span className={ui.srOnly}>Loading deliveries…</span>
        </div>
      )}
      {state.status === "error" && (
        <div role="alert">
          <p className={connect.inlineError} style={{ marginTop: 0 }}>
            Couldn&apos;t load deliveries: {state.message}
          </p>
          <button type="button" className={ui.retry} onClick={onRetry}>
            Try again
          </button>
        </div>
      )}
      {state.status === "ready" && state.data.length === 0 && (
        <p className={ui.muted}>Nothing sent yet. Use “Send test” on a channel to check it works.</p>
      )}
      {state.status === "ready" && state.data.length > 0 && (
        <div className={ui.tableWrap}>
          <table className={ui.table}>
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Channel</th>
                <th scope="col">What</th>
                <th scope="col">Status</th>
                <th scope="col">Error</th>
              </tr>
            </thead>
            <tbody>
              {state.data.map((d) => (
                <tr key={d.id}>
                  <td>
                    <When iso={d.attempted_at} />
                  </td>
                  <td>{d.channel_name ?? names.get(d.channel_id) ?? d.channel_id}</td>
                  <td>{d.alert_event_id ? `Alert #${d.alert_event_id}` : "Digest / test"}</td>
                  <td>
                    <Pill
                      tone={d.status === "sent" ? "ok" : d.status === "failed" ? "critical" : "off"}
                      label={d.status === "sent" ? "Sent" : d.status === "failed" ? "Failed" : d.status}
                    />
                  </td>
                  <td style={{ whiteSpace: "normal", minWidth: 160, maxWidth: 420 }}>
                    {d.error ? <span className={m2.resultErr}>{d.error}</span> : <span className={m2.dim}>–</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
