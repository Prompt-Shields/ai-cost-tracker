import type { Metadata } from "next";
import { ConnectAzure } from "@/components/connect/ConnectAzure";

export const metadata: Metadata = { title: "Connect Azure · AI Cost Tracker" };

export default function Page() {
  return <ConnectAzure />;
}
