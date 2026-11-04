from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from ``ACT_*`` environment variables or ``.env``."""

    model_config = SettingsConfigDict(env_prefix="ACT_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://act:act_local_dev@localhost:5432/ai_cost_tracker"
    redis_url: str = "redis://localhost:6379/0"

    # Entra ID sign-in. The API validates access tokens issued for this app registration.
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    # Only members of these Entra groups may sign in. Empty means anyone in the tenant.
    allowed_group_ids: list[str] = []
    # Entra group object IDs per app role. A user gets every role whose group they are in.
    role_groups: dict[str, list[str]] = {}
    # Local development only: skip token validation and act as a fixed dev user (owner).
    # Refused unless environment == "local".
    auth_disabled: bool = False
    environment: Literal["local", "test", "production"] = "local"
    # Browser origins allowed to call the API (the web app). Locally defaults to
    # http://localhost:3000.
    web_origins: list[str] = []

    # Which connector the worker runs. "fake" replays deterministic demo data.
    connector: Literal["fake", "azure"] = "fake"
    # Subscriptions to track with the Azure connector. Empty means every visible
    # subscription that contains AI resources.
    azure_subscription_ids: list[str] = []
    # Object (principal) ID of the app's managed identity: the identity that is granted
    # Reader on each subscription during "Connect Azure" (ARCHITECTURE.md §4.3).
    azure_principal_id: str = ""
    # Public URL of the web app, used to build the shareable "grant access" link.
    web_base_url: str = "http://localhost:3000"

    # GCP (M3, ARCHITECTURE.md §8). Runs alongside the Azure connector when enabled; the
    # fake connector always brings a demo GCP project. No JSON keys: the app's Azure managed
    # identity is exchanged for a GCP token through Workload Identity Federation.
    gcp_enabled: bool = False
    # Projects to track. Empty means every project the service account can see that uses
    # Vertex AI.
    gcp_project_ids: list[str] = []
    # Full resource name of the WIF provider that trusts our Entra tenant:
    # //iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>
    # /providers/<provider> (one string, no line break).
    gcp_wif_audience: str = ""
    # Service account the federated identity impersonates; projects grant their roles to it.
    gcp_service_account: str = ""
    # Entra scope requested for the subject token handed to Google STS.
    gcp_azure_token_scope: str = "api://AzureADTokenExchange/.default"
    # Detailed billing export table, "<project>.<dataset>.gcp_billing_export_resource_v1_<id>".
    gcp_billing_export_table: str = ""
    # BigQuery location of the billing export dataset (jobs must run there).
    gcp_billing_export_location: str = "US"
    # Vertex AI regions to list endpoints in ("global" covers the global endpoint).
    gcp_regions: list[str] = ["global", "us-central1", "us-east4", "europe-west4"]

    # Currency every total is reported in; other billing currencies are converted with the
    # daily ECB reference rate (fx.py).
    reporting_currency: str = "USD"

    # Email notifications (issue #30). Leave smtp_host empty to disable email channels.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    # From the environment / Key Vault reference only; never in code or committed config.
    smtp_password: str = ""
    smtp_from: str = "ai-cost-tracker@localhost"
    smtp_starttls: bool = True

    # The single internal tenant every row belongs to in v1 (see ARCHITECTURE.md §2a).
    default_tenant_slug: str = "internal"


@lru_cache
def get_settings() -> Settings:
    return Settings()
