import { redirect } from "next/navigation";

/** /settings has one page for now. */
export default function Page() {
  redirect("/settings/api-keys");
}
