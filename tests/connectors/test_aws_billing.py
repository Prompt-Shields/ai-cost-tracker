"""CUR 2.0 Data Export from S3: manifest and fallback listing, filters, netting, aggregation."""

import gzip
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ai_cost_tracker.connectors.aws import billing
from ai_cost_tracker.connectors.aws.ids import ExportLocation, resource_id

FIXTURES = Path(__file__).parent.parent / "fixtures" / "aws" / "billing"
ACCOUNT = "111111111111"
BUCKET = "act-cur-bucket"
EXPORT = ExportLocation(
    account_id="999999999999",
    bucket=BUCKET,
    prefix="ai-cost-tracker",
    name="ai-cost-tracker-cur2",
    region="us-east-1",
)
BASE = "ai-cost-tracker/ai-cost-tracker-cur2"
USE1 = resource_id(ACCOUNT, "us-east-1")
USW2 = resource_id(ACCOUNT, "us-west-2")


class FakeBody:
    """Just enough of botocore's ``StreamingBody``: chunked ``read`` and ``close``."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.closed = False
        self.reads = 0

    def read(self, amt: int | None = None) -> bytes:
        self.reads += 1
        end = len(self._data) if amt is None or amt < 0 else self._pos + amt
        chunk = self._data[self._pos : end]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeSessions:
    """In-memory S3 behind the ``AwsSessions`` interface (no boto3, no network)."""

    def __init__(self, objects: dict[str, tuple[bytes, datetime]] | None = None) -> None:
        self.objects = dict(objects or {})
        self.clients: list[tuple[str, str, str]] = []
        self.gets: list[str] = []
        self.bodies: list[FakeBody] = []

    def put(self, key: str, data: bytes, modified: datetime | None = None) -> None:
        self.objects[key] = (data, modified or datetime(2026, 11, 3, tzinfo=UTC))

    async def client(self, service: str, account_id: str, region: str) -> Any:
        self.clients.append((service, account_id, region))
        return object()

    async def call(self, client: Any, method: str, **kwargs: Any) -> dict[str, Any]:
        assert method == "get_object"
        assert kwargs["Bucket"] == BUCKET
        key = kwargs["Key"]
        self.gets.append(key)
        body = FakeBody(self.objects[key][0])  # KeyError = NoSuchKey: we never guess keys
        self.bodies.append(body)
        return {"Body": body, "ETag": '"x"'}

    async def paginate(
        self, client: Any, method: str, result_key: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        assert (method, result_key) == ("list_objects_v2", "Contents")
        assert kwargs["Bucket"] == BUCKET
        prefix = kwargs["Prefix"]
        return [
            {"Key": k, "LastModified": m, "Size": len(d)}
            for k, (d, m) in sorted(self.objects.items())
            if k.startswith(prefix)
        ]

    async def aclose(self) -> None:
        pass


def gz(name: str) -> bytes:
    return gzip.compress((FIXTURES / name).read_bytes())


def october_with_manifest(s: FakeSessions) -> None:
    s.put(
        f"{BASE}/metadata/BILLING_PERIOD=2026-10/ai-cost-tracker-cur2-Manifest.json",
        (FIXTURES / "2026-10-manifest.json").read_bytes(),
    )
    s.put(
        f"{BASE}/data/BILLING_PERIOD=2026-10/ai-cost-tracker-cur2-00001.csv.gz",
        gz("2026-10-data.csv"),
    )
    # A leftover file the manifest doesn't list must not be read.
    s.put(f"{BASE}/data/BILLING_PERIOD=2026-10/old-00009.csv.gz", gz("2026-11-stale.csv"))


def november_without_manifest(s: FakeSessions) -> None:
    old = datetime(2026, 11, 2, tzinfo=UTC)
    new = datetime(2026, 11, 3, tzinfo=UTC)
    period = f"{BASE}/data/BILLING_PERIOD=2026-11"
    s.put(f"{period}/exec-old/part-00001.csv.gz", gz("2026-11-stale.csv"), old)
    s.put(f"{period}/exec-new/part-00001.csv.gz", gz("2026-11-data-1.csv"), new)
    s.put(f"{period}/exec-new/part-00002.csv.gz", gz("2026-11-data-2.csv"), new)


def by_key(records: list) -> dict[tuple, Any]:
    return {(r.charge_period_start, r.resource_id, r.sku_meter): r for r in records}


async def test_pull_cost_across_periods_manifest_and_fallback():
    s = FakeSessions()
    october_with_manifest(s)
    november_without_manifest(s)

    records = await billing.pull_cost(s, EXPORT, ACCOUNT, date(2026, 10, 30), date(2026, 11, 1))

    assert s.clients == [("s3", "999999999999", "us-east-1")]
    assert not any("old-00009" in k or "exec-old" in k for k in s.gets)
    assert all(b.closed for b in s.bodies)
    got = by_key(records)
    assert set(got) == {
        (date(2026, 10, 30), USE1, "Claude3.7Sonnet-input-tokens"),
        (date(2026, 10, 30), USE1, "Claude3.7Sonnet-output-tokens"),
        (date(2026, 10, 30), USW2, "MP:USW2_InputTokenCount-Units"),
        (date(2026, 10, 31), USE1, "NovaPro-input-tokens"),
        (date(2026, 10, 31), USE1, "NovaPro-output-tokens"),
        (date(2026, 10, 31), USE1, "Credit"),
        (date(2026, 11, 1), USE1, "Claude3.7Sonnet-input-tokens"),
    }
    # Usage + usage - credit, exact to the last digit; tax and other accounts excluded.
    sonnet = got[(date(2026, 10, 30), USE1, "Claude3.7Sonnet-input-tokens")]
    assert sonnet.billed_cost == Decimal("1.6234567891")
    assert sonnet.consumed_quantity == Decimal("1041.15")
    assert sonnet.cloud == "aws"
    assert sonnet.sub_account_id == ACCOUNT
    assert sonnet.service_name == "Amazon Bedrock"
    assert sonnet.billing_currency == "USD"

    mp = got[(date(2026, 10, 30), USW2, "MP:USW2_InputTokenCount-Units")]
    assert mp.service_name == "Claude 3 Haiku (Amazon Bedrock Edition)"
    assert mp.billed_cost == Decimal("0.50")

    free = got[(date(2026, 10, 31), USE1, "NovaPro-output-tokens")]
    assert free.billed_cost == Decimal(0)  # zero-cost rows are kept

    nov = got[(date(2026, 11, 1), USE1, "Claude3.7Sonnet-input-tokens")]
    assert nov.billed_cost == Decimal("0.33")  # newest execution only, both of its files
    assert nov.consumed_quantity == Decimal(110)

    assert sum(r.billed_cost for r in records) == Decimal("2.6334567891")
    assert records == sorted(
        records, key=lambda r: (r.charge_period_start, r.resource_id, r.sku_meter)
    )


async def test_pull_cost_is_deterministic():
    s = FakeSessions()
    october_with_manifest(s)
    first = await billing.pull_cost(s, EXPORT, ACCOUNT, date(2026, 10, 30), date(2026, 10, 31))
    second = await billing.pull_cost(s, EXPORT, ACCOUNT, date(2026, 10, 30), date(2026, 10, 31))
    assert first == second
    assert len(first) == 6


async def test_other_account_sees_only_its_rows():
    s = FakeSessions()
    october_with_manifest(s)
    records = await billing.pull_cost(
        s, EXPORT, "222222222222", date(2026, 10, 1), date(2026, 10, 31)
    )
    assert [(r.sku_meter, r.billed_cost) for r in records] == [
        ("Claude3.7Sonnet-input-tokens", Decimal("1.5"))
    ]


async def test_missing_period_is_empty_not_an_error():
    s = FakeSessions()
    october_with_manifest(s)
    assert await billing.pull_cost(s, EXPORT, ACCOUNT, date(2026, 12, 1), date(2027, 1, 2)) == []
    assert s.gets == []


async def test_empty_manifest_falls_back_to_listing():
    s = FakeSessions()
    s.put(
        f"{BASE}/metadata/BILLING_PERIOD=2026-11/ai-cost-tracker-cur2-Manifest.json",
        json.dumps({"dataFiles": []}).encode(),
    )
    s.put(f"{BASE}/data/BILLING_PERIOD=2026-11/x-00001.csv.gz", gz("2026-11-data-2.csv"))
    records = await billing.pull_cost(s, EXPORT, ACCOUNT, date(2026, 11, 1), date(2026, 11, 1))
    assert [r.billed_cost for r in records] == [Decimal("0.03")]


async def test_plain_csv_bytes_body_and_empty_prefix():
    export = ExportLocation(ACCOUNT, BUCKET, "", "cur", "eu-west-1")
    s = FakeSessions()
    s.put(
        "cur/data/BILLING_PERIOD=2026-11/cur-00001.csv",
        (FIXTURES / "2026-11-data-2.csv").read_bytes(),
    )

    async def call(client: Any, method: str, **kwargs: Any) -> dict[str, Any]:
        return {"Body": s.objects[kwargs["Key"]][0]}  # raw bytes instead of a stream

    s.call = call  # type: ignore[method-assign]
    records = await billing.pull_cost(s, export, ACCOUNT, date(2026, 11, 1), date(2026, 11, 30))
    assert [(r.sku_meter, r.billed_cost) for r in records] == [
        ("Claude3.7Sonnet-input-tokens", Decimal("0.03"))
    ]


async def test_missing_required_column_is_a_clear_error():
    s = FakeSessions()
    s.put(
        f"{BASE}/data/BILLING_PERIOD=2026-11/bad.csv.gz",
        gzip.compress(b"line_item_usage_account_id,line_item_unblended_cost\n111111111111,1\n"),
    )
    with pytest.raises(billing.CurFormatError, match="line_item_usage_start_date"):
        await billing.pull_cost(s, EXPORT, ACCOUNT, date(2026, 11, 1), date(2026, 11, 1))


async def test_parquet_export_is_rejected():
    s = FakeSessions()
    s.put(f"{BASE}/data/BILLING_PERIOD=2026-11/part-0.snappy.parquet", b"PAR1")
    with pytest.raises(billing.CurFormatError, match="CSV"):
        await billing.pull_cost(s, EXPORT, ACCOUNT, date(2026, 11, 1), date(2026, 11, 1))


async def test_end_before_start():
    with pytest.raises(ValueError):
        await billing.pull_cost(
            FakeSessions(), EXPORT, ACCOUNT, date(2026, 11, 2), date(2026, 11, 1)
        )


def test_billing_periods_span_months_and_years():
    assert billing.billing_periods(date(2026, 11, 15), date(2027, 2, 1)) == [
        "2026-11",
        "2026-12",
        "2027-01",
        "2027-02",
    ]
    assert billing.billing_periods(date(2026, 11, 1), date(2026, 11, 30)) == ["2026-11"]


@pytest.mark.parametrize(
    ("usage_type", "expected"),
    [
        ("USE1-Claude3.7Sonnet-input-tokens", "Claude3.7Sonnet-input-tokens"),
        ("EUC1-NovaPro-output-tokens", "NovaPro-output-tokens"),
        ("APN1-Llama3.1-70B-input-tokens", "Llama3.1-70B-input-tokens"),
        ("UGW1-Claude3.5Haiku-input-tokens", "Claude3.5Haiku-input-tokens"),
        ("USW2-MP:USW2_InputTokenCount-Units", "MP:USW2_InputTokenCount-Units"),
        ("Claude3.7Sonnet-input-tokens", "Claude3.7Sonnet-input-tokens"),
        ("NovaPro-input-tokens", "NovaPro-input-tokens"),
    ],
)
def test_strip_region(usage_type, expected):
    assert billing.strip_region(usage_type) == expected


def test_manifest_files_shapes():
    raw = json.dumps(
        {"dataFiles": ["s3://other-bucket/a/b.csv.gz", "/c/d.csv.gz", {"key": "e.csv.gz"}, 3]}
    ).encode()
    assert billing.manifest_files(raw, BUCKET) == [
        ("other-bucket", "a/b.csv.gz"),
        (BUCKET, "c/d.csv.gz"),
        (BUCKET, "e.csv.gz"),
    ]


def test_cur_columns_cover_required_and_are_unique():
    assert set(billing.REQUIRED_COLUMNS) <= set(billing.CUR_COLUMNS)
    assert len(set(billing.CUR_COLUMNS)) == len(billing.CUR_COLUMNS)
    assert all(c == c.lower() and " " not in c for c in billing.CUR_COLUMNS)
