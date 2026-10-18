"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { ConnectApi, type Connection } from "@/lib/api-connect";
import { AdminApi, isAdmin, type InvoicePeriods } from "@/lib/api-m2-admin";
import { formatMoney } from "@/lib/format";
import { useSession } from "../AuthProvider";
import styles from "../settings/admin.module.css";
import { formatDate } from "../settings/format";
import { useMe } from "../settings/useMe";
import { EmptyNotice, ErrorNotice, Skeleton, errorMessage, ui, useLoad } from "../ui";

const DAYS = Array.from({ length: 28 }, (_, i) => i + 1);

/** Per-subscription invoice periods (#36), for reconciling against the Azure invoice. */
export function Invoices() {
  const { api } = useSession();
  const connect = useMemo(() => new ConnectApi(api.getToken), [api]);
  const admin = useMemo(() => new AdminApi(api.getToken), [api]);
  const { me } = useMe();
  const [accounts, retryAccounts] = useLoad(() => connect.connections(), "connections");
  const [accountId, setAccountId] = useState<string>("");
  const [version, setVersion] = useState(0);

  const options = useMemo(() => {
    if (accounts.status !== "ready") return [];
    const tracked = accounts.data.filter((c) => c.tracked);
    return [...(tracked.length ? tracked : accounts.data)].sort((a, b) =>
      label(a).localeCompare(label(b)),
    );
  }, [accounts]);

  useEffect(() => {
    if (!accountId && options.length) setAccountId(options[0].account_id);
  }, [accountId, options]);

  const [periods, retryPeriods] = useLoad<InvoicePeriods | null>(
    () => (accountId ? admin.invoicePeriods(accountId, 12) : Promise.resolve(null)),
    `${accountId}|${version}`,
  );

  return (
    <main className={ui.main}>
      <div className={ui.toolbar}>
        <div>
          <h1 className={ui.title}>Invoices</h1>
          <p className={ui.subtitle}>Cost per invoice period, to check against the Azure invoice.</p>
        </div>
        {options.length > 0 && (
          <label className={styles.field} style={{ minWidth: 0, maxWidth: "100%" }}>
            <span className={styles.fieldLabel}>Subscription</span>
            <select
              className={styles.select}
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
            >
              {options.map((c) => (
                <option key={c.account_id} value={c.account_id}>
                  {label(c)}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {accounts.status === "loading" && <Skeleton height={280} label="Loading subscriptions…" />}
      {accounts.status === "error" && (
        <ErrorNotice
          title="Couldn't load subscriptions"
          message={accounts.message}
          onRetry={retryAccounts}
        />
      )}
      {accounts.status === "ready" && options.length === 0 && (
        <EmptyNotice title="No subscriptions connected">
          Connect an Azure subscription to see its invoice periods.{" "}
          <Link className={ui.link} href="/connect">
            Connect Azure
          </Link>
        </EmptyNotice>
      )}

      {accountId && periods.status === "loading" && <Skeleton height={320} label="Loading periods…" />}
      {accountId && periods.status === "error" && (
        <ErrorNotice
          title="Couldn't load invoice periods"
          message={periods.message}
          onRetry={retryPeriods}
        />
      )}
      {periods.status === "ready" && periods.data && (
        <>
          <PeriodSetting
            key={`${periods.data.account_id}|${periods.data.billing_period_day ?? "cal"}`}
            admin={admin}
            accountId={periods.data.account_id}
            value={periods.data.billing_period_day}
            canEdit={isAdmin(me?.roles)}
            onSaved={() => setVersion((v) => v + 1)}
          />
          <PeriodTable data={periods.data} />
        </>
      )}
    </main>
  );
}

function label(c: Connection): string {
  return c.name ? `${c.name} (${c.account_id.slice(0, 8)}…)` : c.account_id;
}

function describe(day: number | null): string {
  return day === null ? "Calendar month" : `Starts on day ${day} of each month`;
}

function PeriodSetting({
  admin,
  accountId,
  value,
  canEdit,
  onSaved,
}: {
  admin: AdminApi;
  accountId: string;
  value: number | null;
  canEdit: boolean;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<string>(value === null ? "" : String(value));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const changed = draft !== (value === null ? "" : String(value));

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await admin.setBillingPeriodDay(accountId, draft === "" ? null : Number(draft));
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <section className={ui.panel}>
      <h2 className={ui.panelTitle}>Billing period</h2>
      {canEdit ? (
        <form
          className={styles.form}
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <label className={styles.field}>
            <span className={styles.fieldLabel}>Period starts on</span>
            <select
              className={styles.select}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            >
              <option value="">Calendar month (1st)</option>
              {DAYS.map((d) => (
                <option key={d} value={String(d)}>
                  Day {d}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" className={styles.primary} disabled={busy || !changed}>
            {busy ? "Saving…" : "Save"}
          </button>
        </form>
      ) : (
        <p className={ui.muted}>{describe(value)}</p>
      )}
      <p className={styles.hint}>
        Pay-as-you-go invoices often run from the subscription&apos;s sign-up day (for example the
        15th to the 14th). Match the dates on your Azure invoice.
      </p>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

function PeriodTable({ data }: { data: InvoicePeriods }) {
  if (data.periods.length === 0) {
    return (
      <EmptyNotice title="No invoice periods yet">
        Periods appear once billing data has been ingested for this subscription.
      </EmptyNotice>
    );
  }
  return (
    <section className={ui.panel}>
      <h2 className={ui.panelTitle}>Periods</h2>
      <div className={`${ui.tableWrap} ${styles.clip}`}>
        <table className={ui.table}>
          <thead>
            <tr>
              <th scope="col">Period</th>
              <th scope="col" className={ui.num}>Billed</th>
              <th scope="col" className={ui.num}>Estimated</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {data.periods.map((p) => {
              const currency = p.currency || "USD";
              return (
                <tr key={p.start}>
                  <th scope="row">
                    {formatDate(p.start)} – {formatDate(p.end)}
                  </th>
                  <td className={ui.num}>{formatMoney(Number(p.billed_cost), currency)}</td>
                  <td className={ui.num}>
                    {Number(p.estimated_cost) === 0 ? "—" : formatMoney(Number(p.estimated_cost), currency)}
                  </td>
                  <td>
                    {p.status === "open" ? (
                      <span className={styles.pillOpen}>Open</span>
                    ) : (
                      <span className={styles.pill}>Closed</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className={styles.note}>
        Figures are cost before credits. Azure credits, sponsorship offsets and taxes reduce the
        invoice but aren&apos;t included here, so the amount due can be lower. “Estimated” is
        usage not yet billed; an open period can still change.
      </p>
    </section>
  );
}
