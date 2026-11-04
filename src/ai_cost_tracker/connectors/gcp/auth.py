"""Workload Identity Federation: the app's Azure managed identity -> a Google access token.

No JSON keys exist anywhere (ARCHITECTURE.md §8). Three hops, each a plain REST call:

1. Entra issues a token for ``azure_scope`` to the managed identity (``DefaultAzureCredential``,
   or ``az login`` locally).
2. Google STS exchanges it for a federated access token; the WIF provider named by
   ``audience`` trusts our Entra tenant as an OIDC issuer.
3. The federated identity impersonates one service account through IAM Credentials
   ``generateAccessToken``. Projects grant their read-only roles to that service account.

Request and response shapes follow Google's public REST docs for ``sts.googleapis.com/v1/token``
and ``iamcredentials.googleapis.com/v1/...:generateAccessToken``.
"""

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

STS_URL = "https://sts.googleapis.com/v1/token"
IAM_CREDENTIALS = "https://iamcredentials.googleapis.com/v1"
CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
LIFETIME = "3600s"
# Refresh this long before Google's expiry so a request never carries a token that lapses
# in flight.
REFRESH_MARGIN = timedelta(minutes=5)

_JWT = re.compile(r"eyJ[\w-]{4,}\.[\w-]{4,}\.[\w-]*")
_GOOGLE_TOKEN = re.compile(r"ya29\.[\w.-]+")


class GcpAuthError(RuntimeError):
    """WIF is misconfigured or a hop was refused; the message says which and never holds a
    token."""


class WifTokenProvider:
    """Cached Google access token for the impersonated service account.

    The Azure credential is created lazily (on the first ``token()``), so building a client
    from incomplete settings never fails at startup; the first call says what is missing.
    """

    def __init__(
        self,
        *,
        audience: str,
        service_account: str,
        azure_scope: str,
        azure_credential: Any = None,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._audience = _normalise_audience(audience)
        self._service_account = service_account.strip()
        self._azure_scope = azure_scope
        self._credential = azure_credential
        self._owns_credential = azure_credential is None
        self._http = http or httpx.AsyncClient(timeout=30)
        self._owns_http = http is None
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None

    async def token(self) -> str:
        async with self._lock:
            if self._token and self._expires_at and self._clock() < self._expires_at:
                return self._token
            self._check_settings()
            subject = await self._azure_token()
            federated = await self._exchange(subject)
            self._token, expires = await self._impersonate(federated)
            self._expires_at = expires - REFRESH_MARGIN
            return self._token

    def invalidate(self) -> None:
        """Forget the cached token (e.g. after a 401) so the next call fetches a new one."""
        self._token = self._expires_at = None

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()
        if self._owns_credential and self._credential is not None:
            self._credential.close()

    def _check_settings(self) -> None:
        missing = [
            name
            for name, value in (
                ("ACT_GCP_WIF_AUDIENCE", self._audience),
                ("ACT_GCP_SERVICE_ACCOUNT", self._service_account),
                ("ACT_GCP_AZURE_TOKEN_SCOPE", self._azure_scope),
            )
            if not value
        ]
        if missing:
            raise GcpAuthError(f"{', '.join(missing)} not set; GCP access is unavailable")

    async def _azure_token(self) -> str:
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
        try:
            got = await asyncio.to_thread(self._credential.get_token, self._azure_scope)
        except Exception as exc:  # azure-identity raises several unrelated types
            raise GcpAuthError(
                f"could not get an Azure token for {self._azure_scope} "
                f"(managed identity, or `az login` locally): {redact(str(exc))}"
            ) from None
        return got.token

    async def _exchange(self, subject_token: str) -> str:
        # Assumption (spike-gcp): STS accepts the Entra token as-is (subject_token_type jwt)
        # when the provider's allowed audience is the app id URI of azure_scope.
        body = {
            "grant_type": TOKEN_EXCHANGE_GRANT,
            "audience": self._audience,
            "scope": CLOUD_PLATFORM,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "subject_token": subject_token,
            "subject_token_type": JWT_TOKEN_TYPE,
        }
        data = await self._call("Google STS token exchange", "POST", STS_URL, data=body)
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise GcpAuthError("Google STS token exchange returned no access_token")
        return token

    async def _impersonate(self, federated: str) -> tuple[str, datetime]:
        url = (
            f"{IAM_CREDENTIALS}/projects/-/serviceAccounts/{self._service_account}"
            ":generateAccessToken"
        )
        data = await self._call(
            f"impersonating {self._service_account}",
            "POST",
            url,
            json={"scope": [CLOUD_PLATFORM], "lifetime": LIFETIME},
            headers={"Authorization": f"Bearer {federated}"},
        )
        token, expire = data.get("accessToken"), data.get("expireTime")
        if not isinstance(token, str) or not token:
            raise GcpAuthError(f"impersonating {self._service_account} returned no accessToken")
        return token, _parse_expiry(expire, self._clock())

    async def _call(self, what: str, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            resp = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise GcpAuthError(f"{what} failed: {type(exc).__name__}") from None
        if resp.is_error:
            raise GcpAuthError(f"{what} failed ({resp.status_code}): {google_error(resp)}")
        try:
            return resp.json()
        except ValueError:
            raise GcpAuthError(f"{what} returned a non-JSON response") from None


def google_error(resp: httpx.Response) -> str:
    """Google's own error text (STS: ``error_description``; other APIs: ``error.message``),
    with anything token-shaped removed."""
    try:
        body = resp.json()
    except ValueError:
        return redact(resp.text[:300]) or resp.reason_phrase
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            text = " ".join(str(err[k]) for k in ("status", "message") if err.get(k))
        else:
            text = ": ".join(str(v) for v in (err, body.get("error_description")) if v)
        if text:
            return redact(text[:500])
    return resp.reason_phrase


def redact(text: str) -> str:
    """Strip JWTs and Google access tokens from text that goes into errors or logs."""
    return _GOOGLE_TOKEN.sub("{redacted-token}", _JWT.sub("{redacted-token}", text))


def _normalise_audience(audience: str) -> str:
    """Accept the provider name with or without the ``//iam.googleapis.com/`` prefix."""
    audience = audience.strip()
    if audience.startswith("projects/"):
        return f"//iam.googleapis.com/{audience}"
    return audience


def _parse_expiry(value: Any, now: datetime) -> datetime:
    """RFC 3339 ``expireTime``; an unparsable value falls back to the requested lifetime."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return now + timedelta(seconds=int(LIFETIME.rstrip("s")))


__all__ = ["GcpAuthError", "WifTokenProvider", "google_error", "redact"]
