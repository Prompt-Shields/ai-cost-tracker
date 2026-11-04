import type { Metadata } from "next";
import { ConnectGcp } from "@/components/connect/ConnectGcp";

export const metadata: Metadata = { title: "Connect Google Cloud · AI Cost Tracker" };

export default function Page() {
  return <ConnectGcp />;
}
