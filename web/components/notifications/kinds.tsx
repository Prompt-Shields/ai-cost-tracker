import type { ReactNode } from "react";
import type { ChannelKind } from "@/lib/api-m2-alerts";
import { m2 } from "../alerts/shared";

export interface KindInfo {
  label: string;
  targetLabel: string;
  placeholder: string;
  inputType: "email" | "url";
  help: string;
}

export const CHANNEL_KINDS: Record<ChannelKind, KindInfo> = {
  slack: {
    label: "Slack",
    targetLabel: "Incoming webhook URL",
    placeholder: "https://hooks.slack.com/services/…",
    inputType: "url",
    help: "In Slack, add an Incoming Webhook to the channel (Apps › Incoming Webhooks) and paste its URL.",
  },
  teams: {
    label: "Microsoft Teams",
    targetLabel: "Workflows webhook URL",
    placeholder: "https://…logic.azure.com/workflows/…",
    inputType: "url",
    help: "In the Teams channel, open Workflows › “Post to a channel when a webhook request is received” and paste the URL it gives you.",
  },
  email: {
    label: "Email",
    targetLabel: "Email address",
    placeholder: "ai-spend@example.com",
    inputType: "email",
    help: "A person or a distribution list. Sent through the SMTP server configured for the tracker.",
  },
  webhook: {
    label: "Webhook",
    targetLabel: "Webhook URL",
    placeholder: "https://example.com/hooks/ai-cost",
    inputType: "url",
    help: "We POST a JSON body signed with an HMAC-SHA256 secret, shown once after you create the channel.",
  },
};

export const KIND_ORDER: ChannelKind[] = ["slack", "teams", "email", "webhook"];

export function kindLabel(kind: string): string {
  return CHANNEL_KINDS[kind as ChannelKind]?.label ?? kind;
}

const PATHS: Record<ChannelKind, ReactNode> = {
  email: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="m3 7 9 6 9-6" />
    </>
  ),
  slack: (
    <>
      <path d="M9 4 7 20M17 4l-2 16M4 9h16M3 15h16" />
    </>
  ),
  teams: (
    <>
      <path d="M4 5h16v11H9l-5 4z" />
      <path d="M9 9h6M12 9v4" />
    </>
  ),
  webhook: (
    <>
      <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" />
      <path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />
    </>
  ),
};

/** Generic glyphs (no brand marks): envelope, hash, chat bubble, link. */
export function KindIcon({ kind }: { kind: string }) {
  const path = PATHS[kind as ChannelKind] ?? PATHS.webhook;
  return (
    <span className={m2.kindIcon} aria-hidden>
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {path}
      </svg>
    </span>
  );
}
