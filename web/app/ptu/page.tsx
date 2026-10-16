import type { Metadata } from "next";
import { PtuReport } from "@/components/ptu/PtuReport";

export const metadata: Metadata = { title: "PTU utilisation · AI Cost Tracker" };

export default function Page() {
  return <PtuReport />;
}
