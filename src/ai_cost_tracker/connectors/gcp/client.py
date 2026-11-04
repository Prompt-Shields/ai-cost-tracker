"""Async REST client for Google APIs with retries. Owned by W1."""

from typing import Any


class GcpClient:
    def __init__(self, tokens: Any, http: Any = None) -> None:
        raise NotImplementedError

    async def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def post(
        self, url: str, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError
