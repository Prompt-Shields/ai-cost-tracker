"""Azure managed identity -> AWS temporary credentials per account. Owned by W1."""

from typing import Any


class AwsAuthError(Exception):
    """The app couldn't get AWS credentials at all (Azure token or STS failed)."""


class AwsAccessDenied(Exception):
    """AWS answered access denied: role missing, trust policy wrong or permission absent."""


class AwsSessions:
    def __init__(self, *, role_name: str, azure_scope: str, **kwargs: Any) -> None:
        raise NotImplementedError

    async def client(self, service: str, account_id: str, region: str) -> Any:
        raise NotImplementedError

    async def call(self, client: Any, method: str, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    async def paginate(
        self, client: Any, method: str, result_key: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError
