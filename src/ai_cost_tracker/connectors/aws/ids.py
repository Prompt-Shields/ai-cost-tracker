"""Identifiers shared by the AWS modules, so billing rows, token metrics and inventory join.

A Bedrock "resource" is Bedrock in one account and region, the counterpart of an Azure
OpenAI account; a "deployment" is a model used there: an on-demand model or inference
profile as CloudWatch's ``ModelId`` names it (``anthropic.claude-3-5-haiku-20241022-v1:0``,
``us.anthropic.claude-3-7-sonnet-20250219-v1:0``) or a provisioned throughput.
"""

import re
from dataclasses import dataclass

PREFIX = "//bedrock.amazonaws.com/accounts/"
ACCOUNT_ID = re.compile(r"^\d{12}$")


def resource_id(account_id: str, region: str | None) -> str:
    """``//bedrock.amazonaws.com/accounts/<account>/regions/<region>``.

    A missing region (some billing rows) is ``global``. Lowercased so billing, metrics and
    inventory agree whatever casing each API returns.
    """
    reg = (region or "").strip().lower() or "global"
    return f"{PREFIX}{account_id.strip()}/regions/{reg}"


def parse_resource_id(value: str) -> tuple[str, str]:
    """(account, region) from a ``resource_id``; ValueError for anything else."""
    if not value.startswith(PREFIX):
        raise ValueError(f"not an AWS resource id: {value!r}")
    parts = value[len(PREFIX) :].split("/")
    if len(parts) != 3 or parts[1] != "regions" or not parts[0] or not parts[2]:
        raise ValueError(f"not an AWS resource id: {value!r}")
    return parts[0], parts[2]


def role_arn(account_id: str, role_name: str) -> str:
    """The read-only role our CloudFormation template creates in ``account_id``."""
    return f"arn:aws:iam::{account_id}:role/{role_name}"


@dataclass(frozen=True)
class ExportLocation:
    """Where the CUR 2.0 Data Export lands: S3 key ``<prefix>/<name>/data/...``."""

    account_id: str
    bucket: str
    prefix: str
    name: str
    region: str
