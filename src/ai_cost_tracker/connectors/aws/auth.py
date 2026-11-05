"""Azure managed identity -> AWS temporary credentials per account, with no access keys.

ARCHITECTURE.md §8, phase 4. Two hops:

1. Entra issues a token for ``azure_scope`` to the app's managed identity
   (``DefaultAzureCredential``, or ``az login`` locally).
2. STS ``AssumeRoleWithWebIdentity`` swaps it for temporary credentials of the read-only role
   (``ids.role_arn``) that each account creates from our CloudFormation template. The call is
   unsigned: the web identity token is the only proof of who we are.

Credentials are cached per account and refreshed shortly before they expire; every boto3 call
runs in a worker thread so the event loop never blocks on botocore's synchronous HTTP.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ai_cost_tracker.connectors.aws.ids import ACCOUNT_ID, role_arn

log = logging.getLogger(__name__)

SESSION_NAME = "ai-cost-tracker"
DURATION_SECONDS = 3600
# Refresh this long before AWS's expiry so a request never carries credentials that lapse in
# flight.
REFRESH_MARGIN = timedelta(minutes=5)
# Assumption (spike-aws): the regional STS endpoint in us-east-1 accepts
# AssumeRoleWithWebIdentity for any account (STS is global; regional endpoints are active by
# default for us-east-1). Per AWS's STS docs.
STS_REGION = "us-east-1"
# Generous retries: CloudWatch and Bedrock list APIs throttle quickly on bursts.
CLIENT_CONFIG = Config(
    retries={"mode": "standard", "max_attempts": 8},
    connect_timeout=10,
    read_timeout=60,
    user_agent_extra="ai-cost-tracker",
)
STS_CONFIG = Config(
    signature_version=UNSIGNED,
    retries={"mode": "standard", "max_attempts": 3},
    connect_timeout=10,
    read_timeout=30,
    region_name=STS_REGION,
    user_agent_extra="ai-cost-tracker",
)
# Assumption (spike-aws): STS answers these codes when the role or its OIDC trust is missing
# or the token's aud/sub don't match the trust policy (STS API reference,
# AssumeRoleWithWebIdentity errors). They mean "this account hasn't granted us access".
STS_DENIED = frozenset(
    {
        "AccessDenied",
        "InvalidIdentityToken",
        "IDPRejectedClaim",
        "ExpiredToken",
        "ExpiredTokenException",
    }
)
_MAX_PAGES = 10_000

_JWT = re.compile(r"eyJ[\w-]{4,}\.[\w-]{4,}\.[\w-]*")
_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")


class AwsAuthError(Exception):
    """The app couldn't get AWS credentials at all (Azure token or STS failed). The message
    names the step or setting and never holds a token or key."""


class AwsAccessDenied(Exception):
    """AWS answered access denied: role missing, trust policy wrong or permission absent."""


@dataclass
class _Account:
    """One account's temporary session and the clients built on it."""

    session: Any
    refresh_at: datetime
    clients: dict[tuple[str, str], Any] = field(default_factory=dict)


class AwsSessions:
    """Per-account boto3 sessions on credentials from ``AssumeRoleWithWebIdentity``.

    The Azure credential and STS client are created lazily, so building this from incomplete
    settings never fails at startup; the first ``client()`` says what is missing. Get a
    client per operation rather than keeping one: a client holds the credentials it was made
    with, which expire after an hour.
    """

    def __init__(
        self,
        *,
        role_name: str,
        azure_scope: str,
        azure_credential: Any = None,
        sts_client: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._role_name = (role_name or "").strip()
        self._azure_scope = (azure_scope or "").strip()
        self._credential = azure_credential
        self._owns_credential = azure_credential is None
        self._sts = sts_client
        self._owns_sts = sts_client is None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._accounts: dict[str, _Account] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def client(self, service: str, account_id: str, region: str) -> Any:
        """boto3 client for ``service`` in ``region``, acting as our role in ``account_id``."""
        account_id = account_id.strip()
        if not ACCOUNT_ID.match(account_id):
            raise ValueError(f"not a 12-digit AWS account id: {account_id!r}")
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            entry = self._accounts.get(account_id)
            if entry is None or self._clock() >= entry.refresh_at:
                entry = await self._assume(account_id)
                self._accounts[account_id] = entry
            key = (service, region)
            if key not in entry.clients:
                # Under the account lock: creating clients from one Session in parallel threads
                # isn't thread-safe in boto3.
                entry.clients[key] = await asyncio.to_thread(
                    entry.session.client, service, region_name=region, config=CLIENT_CONFIG
                )
            return entry.clients[key]

    async def call(self, client: Any, method: str, **kwargs: Any) -> dict[str, Any]:
        """``client.<method>(**kwargs)`` in a thread; access denied -> ``AwsAccessDenied``."""
        try:
            return await asyncio.to_thread(getattr(client, method), **kwargs)
        except ClientError as exc:
            raise _mapped(exc, client, method) from None

    async def paginate(
        self, client: Any, method: str, result_key: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """All ``result_key`` items across pages: botocore's paginator when the operation has
        one, else a ``NextToken``/``nextToken`` loop."""
        can_paginate = getattr(client, "can_paginate", None)
        try:
            if can_paginate is not None and can_paginate(method):
                return await asyncio.to_thread(_native_pages, client, method, result_key, kwargs)
            return await self._token_loop(client, method, result_key, kwargs)
        except ClientError as exc:
            raise _mapped(exc, client, method) from None

    async def aclose(self) -> None:
        for entry in self._accounts.values():
            for c in entry.clients.values():
                _close(c)
        self._accounts.clear()
        if self._owns_sts and self._sts is not None:
            _close(self._sts)
            self._sts = None
        if self._owns_credential and self._credential is not None:
            _close(self._credential)
            self._credential = None

    async def _token_loop(
        self, client: Any, method: str, result_key: str, kwargs: dict[str, Any]
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        args = dict(kwargs)
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            page = await asyncio.to_thread(getattr(client, method), **args)
            items.extend(page.get(result_key) or [])
            # Most AWS APIs say NextToken; Bedrock's say nextToken.
            name = next((k for k in ("NextToken", "nextToken") if page.get(k)), None)
            if name is None or page[name] in seen:
                return items
            seen.add(page[name])
            args[name] = page[name]
        raise RuntimeError(f"{method}: more than {_MAX_PAGES} pages; giving up")

    async def _assume(self, account_id: str) -> _Account:
        self._check_settings()
        arn = role_arn(account_id, self._role_name)
        token = await self._azure_token()
        sts = await self._sts_client()
        try:
            resp = await asyncio.to_thread(
                sts.assume_role_with_web_identity,
                RoleArn=arn,
                RoleSessionName=SESSION_NAME,
                WebIdentityToken=token,
                DurationSeconds=DURATION_SECONDS,
            )
        except ClientError as exc:
            code, message = _error(exc)
            if code in STS_DENIED:
                raise AwsAccessDenied(
                    f"AWS STS refused AssumeRoleWithWebIdentity into {arn} ({code}): {message}. "
                    "Check the CloudFormation stack in that account (OIDC provider audience, "
                    "role trust policy) and ACT_AWS_ROLE_NAME."
                ) from None
            raise AwsAuthError(
                f"AWS STS AssumeRoleWithWebIdentity into {arn} failed ({code}): {message}"
            ) from None
        except BotoCoreError as exc:
            raise AwsAuthError(
                f"AWS STS AssumeRoleWithWebIdentity into {arn} failed: {type(exc).__name__}"
            ) from None
        creds = resp.get("Credentials") or {}
        if not all(creds.get(k) for k in ("AccessKeyId", "SecretAccessKey", "SessionToken")):
            raise AwsAuthError(
                f"AWS STS AssumeRoleWithWebIdentity into {arn} returned no credentials"
            )
        session = await asyncio.to_thread(
            boto3.session.Session,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        expires = _expiry(creds.get("Expiration"), self._clock())
        log.info("assumed %s; credentials valid until %s", arn, expires.isoformat())
        return _Account(session=session, refresh_at=expires - REFRESH_MARGIN)

    def _check_settings(self) -> None:
        missing = [
            name
            for name, value in (
                ("ACT_AWS_ROLE_NAME", self._role_name),
                ("ACT_AWS_AZURE_TOKEN_SCOPE", self._azure_scope),
            )
            if not value
        ]
        if missing:
            raise AwsAuthError(f"{', '.join(missing)} not set; AWS access is unavailable")

    async def _azure_token(self) -> str:
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
        try:
            got = await asyncio.to_thread(self._credential.get_token, self._azure_scope)
        except Exception as exc:  # azure-identity raises several unrelated types
            raise AwsAuthError(
                f"could not get an Azure token for {self._azure_scope} "
                f"(ACT_AWS_AZURE_TOKEN_SCOPE; managed identity, or `az login` locally): "
                f"{redact(str(exc))}"
            ) from None
        return got.token

    async def _sts_client(self) -> Any:
        if self._sts is None:
            self._sts = await asyncio.to_thread(boto3.client, "sts", config=STS_CONFIG)
        return self._sts


def redact(text: str) -> str:
    """Strip JWTs and AWS access key ids from text that goes into errors or logs."""
    return _ACCESS_KEY.sub("{redacted-key}", _JWT.sub("{redacted-token}", text))


def is_access_denied(code: str) -> bool:
    """AWS's many spellings of "you may not": ``AccessDenied``, ``AccessDeniedException``,
    ``UnauthorizedOperation``, ``AuthorizationError``, ``...NotAuthorized...``."""
    return (
        code.startswith("AccessDenied")
        or code in ("UnauthorizedOperation", "AuthorizationError")
        or "NotAuthorized" in code
    )


def _mapped(exc: ClientError, client: Any, method: str) -> Exception:
    """``AwsAccessDenied`` for denials; the original error otherwise."""
    code, message = _error(exc)
    if not is_access_denied(code):
        return exc
    service = getattr(
        getattr(getattr(client, "meta", None), "service_model", None), "service_name", ""
    )
    what = f"{service}.{method}" if service else method
    return AwsAccessDenied(f"{what} denied ({code}): {message}")


def _error(exc: ClientError) -> tuple[str, str]:
    err = exc.response.get("Error") or {}
    return str(err.get("Code") or "Unknown"), redact(str(err.get("Message") or "")[:500])


def _native_pages(
    client: Any, method: str, result_key: str, kwargs: dict[str, Any]
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in client.get_paginator(method).paginate(**kwargs):
        items.extend(page.get(result_key) or [])
    return items


def _expiry(value: Any, now: datetime) -> datetime:
    """STS ``Expiration`` (botocore parses it to an aware datetime); fallback: the requested
    duration from now."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            value = None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return now + timedelta(seconds=DURATION_SECONDS)


def _close(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # closing is best effort
            log.debug("closing %s failed", type(obj).__name__, exc_info=True)


__all__ = ["AwsAccessDenied", "AwsAuthError", "AwsSessions", "is_access_denied", "redact"]
