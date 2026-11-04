"""GCP projects and Vertex AI deployments (publisher models and self-deployed endpoints).

Shapes follow Google's public REST docs for Resource Manager v3 (``projects.search``,
``projects.get``), Service Usage v1 (``services.get``) and Vertex AI v1
(``projects.locations.endpoints.list``). Not yet seen on a live project:

- Assumption (spike-gcp): the service account's project roles (``roles/aiplatform.viewer``,
  ``roles/monitoring.viewer``) include ``resourcemanager.projects.get``, so a successful
  project GET means access is granted, and no role at all answers 403 (or 404);
- Assumption (spike-gcp): ``serviceusage.services.get`` is in those roles; when it isn't
  (403) the project is treated as not using Vertex AI rather than failing discovery;
- Assumption (spike-gcp): Vertex AI answers 403 (API disabled) or 404 for a region the
  project doesn't use; such regions are skipped.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ai_cost_tracker.connectors.gcp import ids, monitoring
from ai_cost_tracker.core.models import CloudAccount, Deployment
from ai_cost_tracker.normalise import gcp_skus

log = logging.getLogger(__name__)

RESOURCE_MANAGER = "https://cloudresourcemanager.googleapis.com/v3"
SERVICE_USAGE = "https://serviceusage.googleapis.com/v1"
AIPLATFORM_SERVICE = "aiplatform.googleapis.com"
PUBLISHER_LOOKBACK = timedelta(days=30)
DEDICATED_SKU = "dedicated"


def _denied(exc: httpx.HTTPStatusError) -> bool:
    return exc.response.status_code in (403, 404)


async def list_projects(client: Any, *, include_all: bool, only: set[str]) -> list[CloudAccount]:
    """Projects to ingest (or, with ``include_all``, every visible project) with AI flag.

    ``only`` -> just those projects, fetched one by one (invisible ones skipped); otherwise
    every ACTIVE project from ``projects:search``, narrowed to Vertex AI users unless
    ``include_all``.
    """
    if only and not include_all:
        projects = [p for pid in sorted(only) if (p := await _get_project(client, pid))]
    else:
        projects = await _search_projects(client)
    accounts = []
    for project in projects:
        if project.get("state", "ACTIVE") != "ACTIVE":
            continue
        pid = project["projectId"]
        accounts.append(
            CloudAccount(
                cloud="gcp",
                account_id=pid,
                name=project.get("displayName") or pid,
                has_ai_resources=await _vertex_enabled(client, pid),
            )
        )
    if not only and not include_all:
        accounts = [a for a in accounts if a.has_ai_resources]
    return sorted(accounts, key=lambda a: a.account_id)


async def check_access(client: Any, project_id: str) -> bool:
    """GET the project as the service account; 403/404 mean no role (yet)."""
    try:
        await client.get(f"{RESOURCE_MANAGER}/projects/{project_id}")
    except httpx.HTTPStatusError as exc:
        if _denied(exc):
            return False
        raise
    return True


async def list_deployments(client: Any, project_id: str, regions: list[str]) -> list[Deployment]:
    """Publisher models called in the last 30 days plus endpoints with deployed models."""
    end = monitoring.ceil_hour(datetime.now(UTC))
    found: dict[tuple[str, str], Deployment] = {}
    try:
        seen = await monitoring.seen_models(client, project_id, end - PUBLISHER_LOOKBACK, end)
    except httpx.HTTPStatusError as exc:
        if not _denied(exc):
            raise
        log.warning("no Cloud Monitoring access on %s; publisher models unknown", project_id)
        seen = set()
    for location, name in seen:
        dep = _publisher_deployment(project_id, location, name)
        found[(dep.resource_id, dep.name)] = dep
    for region in dict.fromkeys(r.strip().lower() for r in regions if r.strip()):
        for dep in await _endpoints(client, project_id, region):
            found[(dep.resource_id, dep.name)] = dep
    return sorted(found.values(), key=lambda d: (d.resource_id, d.name))


def endpoints_url(project_id: str, region: str) -> str:
    """Vertex AI is regional: ``<region>-aiplatform.googleapis.com``, or the global host."""
    host = (
        "aiplatform.googleapis.com"
        if region == ids.GLOBAL
        else f"{region}-aiplatform.googleapis.com"
    )
    return f"https://{host}/v1/projects/{project_id}/locations/{region}/endpoints"


async def _endpoints(client: Any, project_id: str, region: str) -> list[Deployment]:
    url = endpoints_url(project_id, region)
    deployments: list[Deployment] = []
    token: str | None = None
    while True:
        try:
            page = await client.get(url, params={"pageToken": token} if token else None)
        except httpx.HTTPStatusError as exc:
            if not _denied(exc):
                raise
            log.info("skipping Vertex AI endpoints in %s/%s: %s", project_id, region, exc)
            return deployments
        for endpoint in page.get("endpoints") or []:
            dep = _endpoint_deployment(project_id, region, endpoint)
            if dep is not None:
                deployments.append(dep)
        token = page.get("nextPageToken")
        if not token:
            return deployments


def _publisher_deployment(project_id: str, location: str, name: str) -> Deployment:
    return Deployment(
        cloud="gcp",
        account_id=project_id,
        resource_id=ids.resource_id(project_id, location),
        name=name,
        model=monitoring.canonical(name),
        model_version=gcp_skus.model_version(name),
        sku=None,
        region=location,
    )


def _endpoint_deployment(project_id: str, region: str, endpoint: dict) -> Deployment | None:
    """One deployment per endpoint, named after the deployed model taking most traffic.

    Endpoints without deployed models serve nothing and cost nothing, so they're skipped.
    """
    deployed = endpoint.get("deployedModels") or []
    if not deployed:
        return None
    split = endpoint.get("trafficSplit") or {}
    main = max(deployed, key=lambda m: split.get(m.get("id", ""), 0))
    model_name = main.get("displayName") or (main.get("model") or "").rsplit("/", 1)[-1]
    endpoint_id = (endpoint.get("name") or "").rsplit("/", 1)[-1]
    # The endpoint name carries its location; trust it over the region asked for.
    parts = (endpoint.get("name") or "").split("/")
    location = parts[3] if len(parts) >= 6 and parts[2] == "locations" else region
    return Deployment(
        cloud="gcp",
        account_id=project_id,
        resource_id=ids.resource_id(project_id, location),
        name=f"endpoints/{endpoint_id}",
        model=monitoring.canonical(model_name) if model_name else "unknown",
        model_version=main.get("modelVersionId"),
        sku=DEDICATED_SKU,
        region=location.lower(),
        tags=dict(endpoint.get("labels") or {}),
    )


async def _search_projects(client: Any) -> list[dict]:
    projects: list[dict] = []
    params: dict[str, Any] = {"query": "state:ACTIVE"}
    while True:
        page = await client.get(f"{RESOURCE_MANAGER}/projects:search", params=params)
        projects.extend(page.get("projects") or [])
        token = page.get("nextPageToken")
        if not token:
            return projects
        params = {"query": "state:ACTIVE", "pageToken": token}


async def _get_project(client: Any, project_id: str) -> dict | None:
    try:
        return await client.get(f"{RESOURCE_MANAGER}/projects/{project_id}")
    except httpx.HTTPStatusError as exc:
        if _denied(exc):
            log.warning("project %s is not visible to the service account; skipped", project_id)
            return None
        raise


async def _vertex_enabled(client: Any, project_id: str) -> bool:
    url = f"{SERVICE_USAGE}/projects/{project_id}/services/{AIPLATFORM_SERVICE}"
    try:
        service = await client.get(url)
    except httpx.HTTPStatusError as exc:
        if _denied(exc):
            return False
        raise
    return service.get("state") == "ENABLED"
