import type { Metadata } from "next";
import { GcpGrant } from "@/components/connect/GcpGrant";

export const metadata: Metadata = { title: "Grant project access · AI Cost Tracker" };

export default async function Page({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  return <GcpGrant projectId={decodeURIComponent(projectId)} />;
}
