import type { Metadata } from "next";
import { AwsAccountSetup } from "@/components/connect/AwsAccountSetup";

export const metadata: Metadata = { title: "Set up an AWS account · AI Cost Tracker" };

export default async function Page({ params }: { params: Promise<{ accountId: string }> }) {
  const { accountId } = await params;
  return <AwsAccountSetup accountId={decodeURIComponent(accountId)} />;
}
