import type { Metadata } from "next";
import { Invoices } from "@/components/invoices/Invoices";

export const metadata: Metadata = { title: "Invoices · AI Cost Tracker" };

export default function Page() {
  return <Invoices />;
}
