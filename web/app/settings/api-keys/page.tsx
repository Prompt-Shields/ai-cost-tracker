import type { Metadata } from "next";
import { ApiKeys } from "@/components/settings/ApiKeys";

export const metadata: Metadata = { title: "API keys · AI Cost Tracker" };

export default function Page() {
  return <ApiKeys />;
}
