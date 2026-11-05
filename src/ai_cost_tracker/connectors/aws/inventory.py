"""AWS accounts and Bedrock deployments (on-demand models, inference profiles, provisioned).

Mirrors ``connectors/gcp/inventory.py``. Shapes follow AWS's public API docs for
Organizations (``ListAccounts``, ``DescribeAccount``), STS (``GetCallerIdentity``) and the
Bedrock control plane (``ListInferenceProfiles``, ``ListProvisionedModelThroughputs``,
``ListTagsForResource``). Not yet seen on a live account:

- Assumption (spike-aws): Organizations is a global service reached through ``us-east-1``
  and only answers in the management (or delegated admin) account; elsewhere it says
  AccessDenied or ``AWSOrganizationsNotInUseException``, and we fall back to the configured
  accounts (or the export account alone);
- Assumption (spike-aws): ``ListAccounts`` returns ``State`` (newer) and/or ``Status``
  (deprecated) with ``ACTIVE`` for live accounts;
- Assumption (spike-aws): CloudWatch reports an application inference profile's traffic
  under its ARN (or id) as ``ModelId`` and a provisioned throughput's under its ARN; we
  name the deployment after the matching ``ModelId`` when one was seen so tokens join,
  else after the profile ARN / the provisioned model name;
- Assumption (spike-aws): a region where Bedrock isn't offered fails with botocore's
  ``EndpointConnectionError``; it is skipped like a denied region.
"""

import asyncio
import logging
from typing import Any

from botocore.exceptions import EndpointConnectionError

from ai_cost_tracker.connectors.aws import ids
from ai_cost_tracker.connectors.aws.auth import AwsAccessDenied
from ai_cost_tracker.core.models import CloudAccount, Deployment
from ai_cost_tracker.normalise import aws_usage

log = logging.getLogger(__name__)

GLOBAL_REGION = "us-east-1"  # Organizations and STS are global; any region works for STS.
LOOKBACK_DAYS = 30
INFERENCE_PROFILE_SKU = "InferenceProfile"
PROVISIONED_SKU = "ProvisionedThroughput"
_ORG_NOT_IN_USE = {"AWSOrganizationsNotInUseException", "AccountNotFoundException"}
_CONCURRENCY = 8


def _error_code(exc: Exception) -> str:
    """botocore ``ClientError`` code, or "" for anything else."""
    response = getattr(exc, "response", None) or {}
    return str((response.get("Error") or {}).get("Code") or "")


def _regions(regions: list[str]) -> list[str]:
    return list(dict.fromkeys(r.strip().lower() for r in regions if r.strip()))


async def list_accounts(
    sessions: Any,
    *,
    include_all: bool,
    only: set[str],
    export_account_id: str | None,
    regions: list[str],
) -> list[CloudAccount]:
    """Accounts to ingest (or, with ``include_all``, every organization account) with AI flag.

    ``only`` -> exactly those accounts, even unreadable ones (they show as "waiting");
    otherwise ACTIVE accounts from Organizations through the export account, narrowed to
    Bedrock users unless ``include_all``.
    """
    if only and not include_all:
        names = await _describe_names(sessions, export_account_id, sorted(only))
        found = [(aid, names.get(aid) or aid) for aid in sorted(only)]
    elif export_account_id:
        found = await _organization_accounts(sessions, export_account_id)
    else:
        found = [(aid, aid) for aid in sorted(only)]
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def flagged(account_id: str, name: str) -> CloudAccount:
        async with sem:
            has_ai = await _has_ai_resources(sessions, account_id, _regions(regions))
        return CloudAccount(cloud="aws", account_id=account_id, name=name, has_ai_resources=has_ai)

    accounts = list(await asyncio.gather(*(flagged(a, n) for a, n in found)))
    if not only and not include_all:
        accounts = [a for a in accounts if a.has_ai_resources]
    return sorted(accounts, key=lambda a: a.account_id)


async def check_access(sessions: Any, account_id: str) -> bool:
    """``GetCallerIdentity`` as the assumed role; a denied assume or call means no access."""
    try:
        sts = await sessions.client("sts", account_id, GLOBAL_REGION)
        identity = await sessions.call(sts, "get_caller_identity")
    except AwsAccessDenied as exc:
        log.info("no access to AWS account %s yet: %s", account_id, exc)
        return False
    seen = identity.get("Account")
    if seen and seen != account_id:
        log.warning("role for %s resolved to account %s; treating as no access", account_id, seen)
        return False
    return True


async def list_deployments(sessions: Any, account_id: str, regions: list[str]) -> list[Deployment]:
    """Per region: models seen in the last 30 days, application profiles, provisioned."""
    found: dict[tuple[str, str], Deployment] = {}
    for region in _regions(regions):
        for dep in await _region_deployments(sessions, account_id, region):
            found[(dep.resource_id, dep.name)] = dep
    return sorted(found.values(), key=lambda d: (d.resource_id, d.name))


async def _region_deployments(sessions: Any, account_id: str, region: str) -> list[Deployment]:
    """Each source is read independently so one denied API doesn't hide the others."""
    seen = await _seen_model_ids(sessions, account_id, region, LOOKBACK_DAYS) or []
    profiles = await _bedrock_list(
        sessions,
        account_id,
        region,
        "list_inference_profiles",
        "inferenceProfileSummaries",
        typeEquals="APPLICATION",
    )
    provisioned = await _bedrock_list(
        sessions,
        account_id,
        region,
        "list_provisioned_model_throughputs",
        "provisionedModelSummaries",
        statusEquals="InService",
    )
    rid = ids.resource_id(account_id, region)
    claimed: set[str] = set()
    deployments: list[Deployment] = []
    for profile in profiles:
        dep, ids_used = await _profile_deployment(sessions, account_id, region, profile, seen)
        claimed |= ids_used
        deployments.append(dep)
    for summary in provisioned:
        dep, ids_used = await _provisioned_deployment(sessions, account_id, region, summary, seen)
        claimed |= ids_used
        deployments.append(dep)
    for model_id in seen:
        if model_id in claimed:
            continue
        deployments.append(
            Deployment(
                cloud="aws",
                account_id=account_id,
                resource_id=rid,
                name=model_id,
                model=aws_usage.canonical_model(model_id),
                sku=None,
                region=region,
            )
        )
    return deployments


async def _profile_deployment(
    sessions: Any, account_id: str, region: str, profile: dict, seen: list[str]
) -> tuple[Deployment, set[str]]:
    arn = profile.get("inferenceProfileArn") or ""
    keys = {k for k in (arn, profile.get("inferenceProfileId")) if k}
    matches = [m for m in seen if m in keys]
    models = profile.get("models") or []
    model_arn = (models[0].get("modelArn") if models else "") or ""
    return (
        Deployment(
            cloud="aws",
            account_id=account_id,
            resource_id=ids.resource_id(account_id, region),
            name=matches[0] if matches else arn or profile.get("inferenceProfileId", ""),
            model=aws_usage.canonical_model(model_arn) if model_arn else "unknown",
            sku=INFERENCE_PROFILE_SKU,
            region=region,
            tags=await _tags(sessions, account_id, region, arn),
        ),
        set(matches),
    )


async def _provisioned_deployment(
    sessions: Any, account_id: str, region: str, summary: dict, seen: list[str]
) -> tuple[Deployment, set[str]]:
    arn = summary.get("provisionedModelArn") or ""
    matches = [m for m in seen if m == arn]
    # A custom model's provisioned throughput still bills by its base foundation model.
    model_arn = summary.get("foundationModelArn") or summary.get("modelArn") or ""
    units = summary.get("modelUnits")
    return (
        Deployment(
            cloud="aws",
            account_id=account_id,
            resource_id=ids.resource_id(account_id, region),
            name=matches[0] if matches else summary.get("provisionedModelName") or arn,
            model=aws_usage.canonical_model(model_arn) if model_arn else "unknown",
            sku=PROVISIONED_SKU,
            capacity=int(units) if units is not None else None,
            region=region,
            tags=await _tags(sessions, account_id, region, arn),
        ),
        set(matches),
    )


async def _bedrock_list(
    sessions: Any, account_id: str, region: str, method: str, result_key: str, **kwargs: Any
) -> list[dict]:
    try:
        client = await sessions.client("bedrock", account_id, region)
        return await sessions.paginate(client, method, result_key, **kwargs)
    except (AwsAccessDenied, EndpointConnectionError) as exc:
        log.info("skipping Bedrock %s in %s/%s: %s", method, account_id, region, exc)
        return []


async def _tags(sessions: Any, account_id: str, region: str, arn: str) -> dict[str, str]:
    """Resource tags for allocation; missing tag permission just means no tags."""
    if not arn:
        return {}
    try:
        client = await sessions.client("bedrock", account_id, region)
        response = await sessions.call(client, "list_tags_for_resource", resourceARN=arn)
    except (AwsAccessDenied, EndpointConnectionError) as exc:
        log.info("no tags for %s: %s", arn, exc)
        return {}
    return {t["key"]: t.get("value", "") for t in response.get("tags") or [] if t.get("key")}


async def _seen_model_ids(sessions: Any, account_id: str, region: str, days: int) -> list[str]:
    """W3's ``metrics.seen_model_ids``; empty when it isn't there yet or the region is denied."""
    try:
        from ai_cost_tracker.connectors.aws import metrics

        seen_model_ids = metrics.seen_model_ids  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        return []
    try:
        return list(await seen_model_ids(sessions, account_id, region, days=days))
    except NotImplementedError:
        return []
    except (AwsAccessDenied, EndpointConnectionError) as exc:
        log.info("no Bedrock metrics in %s/%s: %s", account_id, region, exc)
        return []


async def _has_ai_resources(sessions: Any, account_id: str, regions: list[str]) -> bool:
    for region in regions:
        if await _seen_model_ids(sessions, account_id, region, LOOKBACK_DAYS):
            return True
    return False


async def _org_client(sessions: Any, export_account_id: str) -> Any:
    return await sessions.client("organizations", export_account_id, GLOBAL_REGION)


async def _organization_accounts(sessions: Any, export_account_id: str) -> list[tuple[str, str]]:
    """(id, name) of ACTIVE organization accounts; just the export account outside an org."""
    try:
        client = await _org_client(sessions, export_account_id)
        rows = await sessions.paginate(client, "list_accounts", "Accounts")
    except AwsAccessDenied as exc:
        log.warning("Organizations not readable from %s: %s", export_account_id, exc)
        return [(export_account_id, export_account_id)]
    except Exception as exc:
        if _error_code(exc) not in _ORG_NOT_IN_USE:
            raise
        log.info("%s is not in an organization; only it is listed", export_account_id)
        return [(export_account_id, export_account_id)]
    out = []
    for row in rows:
        state = (row.get("State") or row.get("Status") or "ACTIVE").upper()
        if state != "ACTIVE" or not ids.ACCOUNT_ID.match(row.get("Id", "")):
            continue
        out.append((row["Id"], row.get("Name") or row["Id"]))
    return out


async def _describe_names(
    sessions: Any, export_account_id: str | None, account_ids: list[str]
) -> dict[str, str]:
    """Account names via ``DescribeAccount``; stops asking after the first denial."""
    if not export_account_id:
        return {}
    names: dict[str, str] = {}
    try:
        client = await _org_client(sessions, export_account_id)
    except AwsAccessDenied as exc:
        log.info("no Organizations access from %s: %s", export_account_id, exc)
        return names
    for account_id in account_ids:
        try:
            response = await sessions.call(client, "describe_account", AccountId=account_id)
        except AwsAccessDenied as exc:
            log.info("no Organizations access from %s: %s", export_account_id, exc)
            return names
        except Exception as exc:
            code = _error_code(exc)
            if code in _ORG_NOT_IN_USE:
                if code == "AWSOrganizationsNotInUseException":
                    return names
                continue
            raise
        name = (response.get("Account") or {}).get("Name")
        if name:
            names[account_id] = name
    return names
