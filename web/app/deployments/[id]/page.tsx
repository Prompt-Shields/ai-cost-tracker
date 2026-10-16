import type { Metadata } from "next";
import { DeploymentDetail } from "@/components/deployments/DeploymentDetail";

export const metadata: Metadata = { title: "Deployment · AI Cost Tracker" };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <DeploymentDetail id={id} />;
}
