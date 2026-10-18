import type { Metadata } from "next";
import { AlertsInbox } from "@/components/alerts/AlertsInbox";

export const metadata: Metadata = { title: "Alerts · AI Cost Tracker" };

export default function Page() {
  return <AlertsInbox />;
}
