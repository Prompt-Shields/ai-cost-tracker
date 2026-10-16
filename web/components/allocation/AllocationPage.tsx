"use client";

import { useMemo } from "react";
import { AllocApi } from "@/lib/api-m2-alloc";
import { useSession } from "../AuthProvider";
import { ErrorNotice, Skeleton, ui, useLoad } from "../ui";
import m2 from "./m2.module.css";
import { RulesSection } from "./RulesSection";
import { SplitsSection } from "./SplitsSection";
import { UntaggedSection } from "./UntaggedSection";
import { useIsAdmin } from "./useIsAdmin";

/** Allocation settings: rules (first match by priority), split rules, untagged-spend report. */
export function AllocationPage() {
  const { api } = useSession();
  const alloc = useMemo(() => new AllocApi(api), [api]);
  const admin = useIsAdmin();
  const [deployments, retryDeployments] = useLoad(() => api.deployments(), "deployments");
  const [rules, retryRules] = useLoad(() => alloc.rules(), "rules");
  const [splits, retrySplits] = useLoad(() => alloc.splits(), "splits");

  return (
    <main className={`${ui.main} ${m2.root}`}>
      <div className={ui.toolbar}>
        <div>
          <h1 className={ui.title}>Allocation</h1>
          <p className={ui.subtitle}>
            Rules map deployments to a cost centre, team and app. The lowest priority number that
            matches wins; a split rule on a deployment overrides its rules.
          </p>
        </div>
      </div>

      {deployments.status === "error" && (
        <ErrorNotice
          title="Couldn't load deployments"
          message={deployments.message}
          onRetry={retryDeployments}
        />
      )}

      {rules.status === "loading" && <Skeleton height={240} label="Loading rules…" />}
      {rules.status === "error" && (
        <ErrorNotice title="Couldn't load allocation rules" message={rules.message} onRetry={retryRules} />
      )}
      {rules.status === "ready" && (
        <RulesSection
          alloc={alloc}
          admin={admin}
          rules={rules.data}
          deployments={deployments.status === "ready" ? deployments.data : []}
          splits={splits.status === "ready" ? splits.data : []}
          onChanged={retryRules}
        />
      )}

      {splits.status === "loading" && <Skeleton height={160} label="Loading split rules…" />}
      {splits.status === "error" && (
        <ErrorNotice title="Couldn't load split rules" message={splits.message} onRetry={retrySplits} />
      )}
      {splits.status === "ready" && (
        <SplitsSection
          alloc={alloc}
          admin={admin}
          splits={splits.data}
          deployments={deployments.status === "ready" ? deployments.data : []}
          onChanged={retrySplits}
        />
      )}

      <UntaggedSection alloc={alloc} />
    </main>
  );
}
