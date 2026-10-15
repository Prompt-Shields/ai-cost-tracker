import type { Metadata } from "next";
import { PendingAccess } from "@/components/connect/PendingAccess";

export const metadata: Metadata = { title: "Approve access · AI Cost Tracker" };

export default async function Page({ params }: { params: Promise<{ subscriptionId: string }> }) {
  const { subscriptionId } = await params;
  return <PendingAccess subscriptionId={decodeURIComponent(subscriptionId)} />;
}
