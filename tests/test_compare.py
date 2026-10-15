"""``act compare-cost-analysis``: CSV variants, 1% rule, tenant scoping, CLI exit codes."""

import json
import sqlite3
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from ai_cost_tracker import cli
from ai_cost_tracker.config import get_settings
from ai_cost_tracker.db import session as db_session
from ai_cost_tracker.db.models import Base, CostLine, Tenant
from ai_cost_tracker.tools import compare

SUB = "11111111-1111-1111-1111-111111111111"
OTHER_SUB = "22222222-2222-2222-2222-222222222222"
ACCT = (
    f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/"
    "Microsoft.CognitiveServices/accounts/aoai-eastus"
)
FOUNDRY = (
    f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/"
    "Microsoft.MachineLearningServices/workspaces/hub"
)
VM = f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/Microsoft.Compute/virtualMachines/vm1"

# What the portal's Cost analysis "Download > CSV" gives for a PAYG subscription.
PAYG_CSV = f"""﻿UsageDate,ResourceId,Meter,PreTaxCost,Currency
20261012,{ACCT},gpt-4o-0806 Inp glbl,10.00,USD
20261012,{ACCT},gpt-4o-0806 Outp glbl,5.00,USD
20261012,{FOUNDRY},Llama Inp,1.00,USD
20261012,{VM},D2s v5,99.00,USD
20261013,{ACCT},gpt-4o-0806 Inp glbl,20.00,USD
"""


async def add_cost(session, tenant_id, day: date, cost: str, resource=ACCT, sub=SUB, meter="m"):
    session.add(
        CostLine(
            tenant_id=tenant_id,
            cloud="azure",
            sub_account_id=sub,
            resource_id=resource.lower(),
            charge_period_start=day,
            sku_meter=meter,
            billed_cost=Decimal(cost),
            billing_currency="USD",
        )
    )
    await session.commit()


def test_parse_payg_export_skips_untracked_resources():
    export = compare.parse_export(PAYG_CSV)
    assert export.cost_column == "PreTaxCost"
    assert export.currencies == {"USD"}
    assert export.skipped_rows == 1  # the VM
    assert export.first_day == date(2026, 10, 12) and export.last_day == date(2026, 10, 13)
    totals = compare.export_totals(export, date(2026, 10, 12), date(2026, 10, 13))
    assert totals.by_day == {
        date(2026, 10, 12): Decimal("16.00"),
        date(2026, 10, 13): Decimal("20.00"),
    }
    assert totals.by_resource[ACCT.lower()] == Decimal("35.00")


def test_all_resources_keeps_everything():
    assert compare.parse_export(PAYG_CSV, all_resources=True).skipped_rows == 0


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Date,ResourceId,Cost,CostUSD,Currency", "Cost"),  # MCA: billing currency first
        ("UsageDate,ResourceId,CostUSD,Currency", "CostUSD"),
        ("Usage Date,Resource Id,Pre Tax Cost,Currency", "Pre Tax Cost"),
        ("UsageDateTime,InstanceId,PreTaxCost,Currency", "PreTaxCost"),
    ],
)
def test_header_variants(header, expected):
    ncols = len(header.split(","))
    row = ["2026-10-12", ACCT, "1.5", "1.6", "USD"][: ncols - 1] + ["USD"]
    if ncols == 4:
        row = ["2026-10-12", ACCT, "1.5", "USD"]
    export = compare.parse_export(f"{header}\n{','.join(row)}\n")
    assert export.cost_column == expected
    assert export.rows[0].cost == Decimal("1.5")
    assert export.rows[0].resource_id == ACCT.lower()


def test_cost_column_override():
    text = f"Date,ResourceId,Cost,CostUSD\n2026-10-12,{ACCT},1.5,1.7\n"
    assert compare.parse_export(text, cost_column="CostUSD").rows[0].cost == Decimal("1.7")


@pytest.mark.parametrize(
    ("value", "dayfirst", "expected"),
    [
        ("20261012", False, date(2026, 10, 12)),
        ("2026-10-12", False, date(2026, 10, 12)),
        ("2026-10-12T00:00:00Z", False, date(2026, 10, 12)),
        ("2026/10/12", False, date(2026, 10, 12)),
        ("10/12/2026", False, date(2026, 10, 12)),
        ("12/10/2026", True, date(2026, 10, 12)),
        ("12.10.2026", True, date(2026, 10, 12)),
        ("10/12/2026 12:00:00 AM", False, date(2026, 10, 12)),
        ("Oct 12, 2026", False, date(2026, 10, 12)),
        ("12-Oct-2026", False, date(2026, 10, 12)),
    ],
)
def test_parse_day(value, dayfirst, expected):
    assert compare.parse_day(value, dayfirst) == expected


def test_parse_day_rejects_garbage():
    with pytest.raises(ValueError):
        compare.parse_day("yesterday")


def test_dayfirst_detected_from_the_file():
    text = "Date,Cost\n13/10/2026,1\n12/10/2026,2\n"
    export = compare.parse_export(text)
    assert {r.day for r in export.rows} == {date(2026, 10, 13), date(2026, 10, 12)}
    assert not export.warnings or "ResourceId" in export.warnings[0]


def test_ambiguous_dates_warn():
    export = compare.parse_export("Date,Cost\n10/11/2026,1\n")
    assert export.rows[0].day == date(2026, 10, 11)
    assert any("--dayfirst" in w for w in export.warnings)


def test_semicolon_export_with_decimal_comma_and_preamble():
    text = (
        "Cost analysis export\nScope;Subscription\n\n"
        f"Datum;ResourceId;Cost;Currency\n12.10.2026;{ACCT};1.234,56;EUR\n"
    )
    # "Datum" isn't a known header; this file needs the date column to be named.
    with pytest.raises(ValueError, match="no header row"):
        compare.parse_export(text)
    export = compare.parse_export(text.replace("Datum", "Date"), dayfirst=True)
    assert export.rows[0].cost == Decimal("1234.56")
    assert export.rows[0].day == date(2026, 10, 12)
    assert export.currencies == {"EUR"}


def test_subscription_filter_on_export():
    other = ACCT.replace(SUB, OTHER_SUB)
    text = f"Date,ResourceId,Cost\n2026-10-12,{ACCT},1\n2026-10-12,{other},2\n"
    export = compare.parse_export(text, subscription_ids=[SUB])
    assert [r.cost for r in export.rows] == [Decimal(1)]


def test_diff_rules():
    tol, floor = Decimal(1), Decimal("0.01")
    assert not compare.Diff("d", Decimal(100), Decimal("100.99")).over(tol, floor)
    assert compare.Diff("d", Decimal(100), Decimal("101.01")).over(tol, floor)
    assert compare.Diff("d", Decimal(0), Decimal(5)).pct is None
    assert compare.Diff("d", Decimal(0), Decimal(5)).over(tol, floor)
    # Sub-cent differences never fail, however large in percent.
    assert not compare.Diff("d", Decimal("0.003"), Decimal("0.004")).over(tol, floor)


async def test_within_one_percent_passes(session, tenant_id):
    await add_cost(session, tenant_id, date(2026, 10, 12), "10.05", meter="in")
    await add_cost(session, tenant_id, date(2026, 10, 12), "5.00", meter="out")
    await add_cost(session, tenant_id, date(2026, 10, 12), "1.00", resource=FOUNDRY)
    await add_cost(session, tenant_id, date(2026, 10, 13), "19.90")
    result = await compare.run_comparison(session, "internal", compare.parse_export(PAYG_CSV))
    assert result.passed
    assert [d.key for d in result.days] == ["2026-10-12", "2026-10-13"]
    assert result.resources[0].key == ACCT.lower()
    table = compare.render_table(result)
    assert "PASS: 0 of 2 days" in table
    assert "rg-ai/aoai-eastus" in table
    assert any("skipped 1 export rows" in w for w in result.warnings)


async def test_over_one_percent_fails_and_missing_day_counts(session, tenant_id):
    await add_cost(session, tenant_id, date(2026, 10, 12), "17.00")  # +6.25%
    # 2026-10-13 never ingested: 100% off.
    result = await compare.run_comparison(session, "internal", compare.parse_export(PAYG_CSV))
    assert not result.passed
    assert len(result.failed_days) == 2
    data = compare.to_json(result)
    assert data["passed"] is False
    first = data["days"][0]
    assert (first["day"], first["cost_analysis"], first["diff_pct"]) == (
        "2026-10-12",
        "16.00",
        "6.2500",
    )
    assert Decimal(first["ours"]) == 17 and first["over"] is True
    assert data["days"][1]["ours"] == "0" and data["days"][1]["over"] is True


async def test_scoped_to_tenant_and_range(session, tenant_id):
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.commit()
    await add_cost(session, other.id, date(2026, 10, 12), "500")
    await add_cost(session, tenant_id, date(2026, 10, 11), "500")  # outside the range
    await add_cost(session, tenant_id, date(2026, 10, 12), "16.00")
    await add_cost(session, tenant_id, date(2026, 10, 13), "20.00")
    result = await compare.run_comparison(session, "internal", compare.parse_export(PAYG_CSV))
    assert result.passed, compare.render_table(result)


async def test_explicit_range_and_subscription(session, tenant_id):
    await add_cost(session, tenant_id, date(2026, 10, 12), "16.00")
    await add_cost(session, tenant_id, date(2026, 10, 12), "9.00", sub=OTHER_SUB, resource="x")
    result = await compare.run_comparison(
        session,
        "internal",
        compare.parse_export(PAYG_CSV, subscription_ids=[SUB]),
        start=date(2026, 10, 12),
        end=date(2026, 10, 12),
        subscription_ids=[SUB],
    )
    assert result.passed
    assert [d.key for d in result.days] == ["2026-10-12"]


async def test_unknown_tenant_is_an_error(session):
    with pytest.raises(ValueError, match="no tenant"):
        await compare.run_comparison(session, "internal", compare.parse_export(PAYG_CSV))


def test_currency_mismatch_warns():
    theirs = compare.Totals({date(2026, 1, 1): Decimal(1)}, {}, {"EUR"})
    ours = compare.Totals({date(2026, 1, 1): Decimal(1)}, {}, {"USD"})
    result = compare.compare(theirs, ours, date(2026, 1, 1), date(2026, 1, 1))
    assert any("currency differs" in w for w in result.warnings)


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """A SQLite file with the schema and one tenant, wired in as ACT_DATABASE_URL."""
    path = tmp_path / "act.db"
    monkeypatch.setenv("ACT_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    get_settings.cache_clear()
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()
    Base.metadata.create_all(create_engine(f"sqlite:///{path}"))
    tenant = uuid.uuid4()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into tenant (id, slug, name) values (?, 'internal', 'Internal')", (tenant.hex,)
        )
    yield path, tenant
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()
    get_settings.cache_clear()


def insert_cost(path, tenant: uuid.UUID, day: str, cost: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into cost_line (tenant_id, cloud, sub_account_id, resource_id, "
            "charge_period_start, sku_meter, billed_cost, billing_currency) "
            "values (?, 'azure', ?, ?, ?, 'm', ?, 'USD')",
            (tenant.hex, SUB, ACCT.lower(), day, cost),
        )


@pytest.mark.parametrize(("cost13", "code"), [("20.10", 0), ("21.00", 1)])
def test_cli_exit_code(sqlite_db, tmp_path, capsys, cost13, code):
    path, tenant = sqlite_db
    insert_cost(path, tenant, "2026-10-12", "15.00")
    insert_cost(path, tenant, "2026-10-13", cost13)
    csv_path = tmp_path / "export.csv"
    csv_path.write_text(PAYG_CSV.replace(f"20261012,{FOUNDRY},Llama Inp,1.00,USD\n", ""))
    assert cli.main(["compare-cost-analysis", "--csv", str(csv_path)]) == code
    out = capsys.readouterr().out
    assert ("PASS" if code == 0 else "FAIL") in out


def test_cli_json_output(sqlite_db, tmp_path, capsys):
    path, tenant = sqlite_db
    insert_cost(path, tenant, "2026-10-12", "15.00")
    csv_path = tmp_path / "export.csv"
    csv_path.write_text(f"UsageDate,ResourceId,Cost\n20261012,{ACCT},15.00\n")
    assert cli.main(["compare-cost-analysis", "--csv", str(csv_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["passed"] is True and data["days"][0]["day"] == "2026-10-12"


def test_cli_bad_input_exits_2(tmp_path, capsys):
    csv_path = tmp_path / "export.csv"
    csv_path.write_text("foo,bar\n1,2\n")
    assert cli.main(["compare-cost-analysis", "--csv", str(csv_path)]) == 2
    assert "no header row" in capsys.readouterr().err
