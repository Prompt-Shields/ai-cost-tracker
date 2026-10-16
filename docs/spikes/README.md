# Spikes and validation runs

Two operator tools check the Azure connector against reality. Both are read-only.

| Command | Issue | What it answers |
|---------|-------|-----------------|
| `act spike-azure` | #2 | Do the Azure APIs behave the way the connector assumes? |
| `act compare-cost-analysis` | #27 | Do our numbers match Cost Analysis within 1% per day? (M1 exit criterion) |

## 1. Azure spike (`act spike-azure`, issue #2)

The connector was written from Microsoft's REST docs. Every behaviour we haven't seen on a
live pay-as-you-go subscription is marked "#2" in a docstring in
`src/ai_cost_tracker/connectors/azure/*.py` (and in `pricing/retail.py`). The spike checks
each one with **your own `az login`** and writes a report.

### Run it

You need `Reader` on one pay-as-you-go subscription that has at least one Azure OpenAI or
AI Services account with some recent usage.

```bash
az login                                    # once; the spike uses DefaultAzureCredential
uv run act spike-azure --subscription <subscription-id>
# options: --out docs/spikes/azure-payg.md (default)  --days 3 (cost range)
#          --account <resource id of the AI account to probe>  --model gpt-4o-mini
```

It takes under a minute and makes about 20 read calls (one may be retried if Cost
Management throttles). Nothing is created or changed in Azure. It prints one line per
assumption and writes the Markdown report to `--out`.

If no AI cost shows up in the range (new subscription, quiet days), rerun with
`--days 14`; checks that need rows say so in the report instead of guessing.

### What it checks

| Area | Check |
|------|-------|
| Cost Management Query API | `Cost` vs `PreTaxCost` (which works; column names and types; `UsageDate` type; `Currency` column; `nextLink`, followed once when present) |
| | three grouping dimensions (expected: rejected), `ResourceType` filter in lower vs camel case, 13-month range (expected: rejected) vs 1 month |
| | throttling headers on every Cost Management response (`Retry-After`, `x-ms-ratelimit-*`) |
| Resource Graph | subscriptions query (row count, `$skipToken`), Cognitive Services account kinds and counts, `location`, whether `*/deployments` is an indexed type |
| ARM | deployments list for one account (API version, `sku.name` values, model format/name); deployments on non-OpenAI kinds |
| Azure Monitor | `metricDefinitions` for one account (every metric with unit and dimensions, token metrics in bold), then the connector's exact metrics call: configured names, `ModelDeploymentName eq '*'`, `top=1000`, 7-day span, hourly alignment |
| Retail Prices API | one model with our filter and a broad one: service / product names, meter names, `unitOfMeasure`, whether each meter parses |

Each check is isolated: a failing call is recorded and the next check still runs. If there is
no credential at all, the Azure checks are marked "couldn't check" and the command exits 1.

### Reading the report

- **Assumptions**: one row per assumption with `confirmed`, **`different`** (what we saw)
  or `couldn't check` (why).
- **Constants to update**: file, constant, current value and what was observed. Make these
  changes in a follow-up PR, then rerun the spike to confirm.
- **Checks**: notes and sample payloads for each check.
- **HTTP calls**: every call with its status and any throttling headers.

**Sanitising.** Subscription and tenant ids, resource group, account, deployment and
subscription names, emails, tag values, `$skiptoken` values and anything that looks like
a token are replaced with placeholders such as `{subscription-id}` or `{account-1}`.
Model names, kinds, SKUs, regions and meter names are kept, because they're what the report
is about. The access token is never read by the spike code, logged or written. Still, read the
report before committing it to `docs/spikes/`.

## 2. Validate against Cost Analysis (`act compare-cost-analysis`, issue #27)

M1 is done when yesterday's AI cost per deployment is within 1% of Cost Analysis. Run this
comparison every day for two weeks after the 12-month backfill.

### Produce the export

In the Azure portal, for each tracked subscription (or a management group above them):

1. **Cost Management → Cost analysis**, scope = the subscription.
2. View **Accumulated costs** (or **Daily costs**) and set **Granularity: Daily**.
3. **Group by: Resource**. Grouping by meter as well is optional; the tool sums rows.
4. Filter **Resource type** to `microsoft.cognitiveservices/accounts` and
   `microsoft.machinelearningservices/workspaces`. Optional: the tool skips other resource
   types anyway, as long as a `ResourceId` column is present.
5. Pick a date range of complete days (for example the last 14 days, ending yesterday).
6. **Download → CSV** (choose "Download data to CSV", not the chart image).

The tool reads the common variants of this file:

- cost column: `Cost`, `PreTaxCost`, `CostInBillingCurrency`, `CostUSD`, `PreTaxCostUSD`
  (billing currency first, because that's what we store). Use `--cost-column` to pick one;
- date column: `UsageDate`, `Date` or `UsageDateTime`, as `20261012`, `2026-10-12`,
  `2026-10-12T00:00:00Z`, `10/12/2026`, `12/10/2026` (day first is detected when a day is
  above 12; force it with `--dayfirst`) or `Oct 12, 2026`;
- resource column: `ResourceId` or `InstanceId` (optional; without it only per-day totals are
  compared);
- `,` or `;` delimiters (`;` files are read with decimal commas), a UTF-8 BOM and a few
  preamble lines above the header.

### Compare

```bash
uv run act compare-cost-analysis --csv ~/Downloads/costanalysis.csv
# options: --start / --end (default: the export's date range)
#          --subscription <id> (repeatable; limits both sides)
#          --tolerance 1 (percent per day)  --min-abs 0.01 (differences up to a cent are ignored)
#          --json  --all-resources (don't skip non-AI resource types)
```

It reads `CostLine` for the default tenant (`ACT_DEFAULT_TENANT_SLUG`) from the database
in `ACT_DATABASE_URL`, then prints:

- one row per day: Cost Analysis, ours, difference, difference in % of Cost Analysis, and
  `OVER` when it is beyond the tolerance. A day missing on our side counts as 100% off;
- the 20 resources with the largest difference over the whole range;
- warnings (currency mismatch, ambiguous dates, rows skipped).

Exit code: `0` all days within tolerance, `1` at least one day over, `2` unusable input.

### Two-week validation log

Re-export and run the comparison each morning; keep a short log in the issue:

| Run date | Range | Days over 1% | Largest daily diff | Notes (restatement? missing meter?) |
|----------|-------|--------------|--------------------|--------------------------------------|

Usual causes of a mismatch: the export's date range includes today (still incomplete),
Azure restated recent days after our last `cost.pull` (run `act ingest-once` and compare
again), a currency mismatch (`Cost` vs `CostUSD`), or resources outside the tracked types.
