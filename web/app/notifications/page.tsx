import type { Metadata } from "next";
import { Notifications } from "@/components/notifications/Notifications";

export const metadata: Metadata = { title: "Notifications · AI Cost Tracker" };

export default function Page() {
  return <Notifications />;
}
