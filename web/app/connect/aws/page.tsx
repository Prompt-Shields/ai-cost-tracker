import type { Metadata } from "next";
import { ConnectAws } from "@/components/connect/ConnectAws";

export const metadata: Metadata = { title: "Connect AWS · AI Cost Tracker" };

export default function Page() {
  return <ConnectAws />;
}
