import type { Metadata } from "next";
import { AllocationPage } from "@/components/allocation/AllocationPage";

export const metadata: Metadata = { title: "Allocation · AI Cost Tracker" };

export default function Page() {
  return <AllocationPage />;
}
