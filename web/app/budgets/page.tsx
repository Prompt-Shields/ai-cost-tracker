import type { Metadata } from "next";
import { Budgets } from "@/components/budgets/Budgets";

export const metadata: Metadata = { title: "Budgets · AI Cost Tracker" };

export default function Page() {
  return <Budgets />;
}
