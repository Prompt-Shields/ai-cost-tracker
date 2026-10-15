import type { Metadata } from "next";
import { Connections } from "@/components/connect/Connections";

export const metadata: Metadata = { title: "Connections · AI Cost Tracker" };

export default function Page() {
  return <Connections />;
}
