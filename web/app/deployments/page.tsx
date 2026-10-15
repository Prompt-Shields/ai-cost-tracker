import type { Metadata } from "next";
import { DeploymentsList } from "@/components/deployments/DeploymentsList";

export const metadata: Metadata = { title: "Deployments · AI Cost Tracker" };

export default function Page() {
  return <DeploymentsList />;
}
