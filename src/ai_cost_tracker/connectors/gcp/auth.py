"""Workload Identity Federation: Azure managed identity -> Google access token. Owned by W1."""


class WifTokenProvider:
    def __init__(self, *, audience: str, service_account: str, azure_scope: str) -> None:
        raise NotImplementedError

    async def token(self) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
