import type { Metadata } from "next";
import { CostCentres } from "@/components/allocation/CostCentres";

export const metadata: Metadata = { title: "Cost centres · AI Cost Tracker" };

export default function Page() {
  return <CostCentres />;
}
