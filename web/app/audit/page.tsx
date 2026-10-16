import type { Metadata } from "next";
import { AuditLog } from "@/components/audit/AuditLog";

export const metadata: Metadata = { title: "Audit log · AI Cost Tracker" };

export default function Page() {
  return <AuditLog />;
}
