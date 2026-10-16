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

    # The single internal tenant every row belongs to in v1 (see ARCHITECTURE.md §2a).
    default_tenant_slug: str = "internal"


@lru_cache
def get_settings() -> Settings:
    return Settings()
