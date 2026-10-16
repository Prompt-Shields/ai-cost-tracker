from types import SimpleNamespace

from sqlalchemy import select

from ai_cost_tracker import audit
from ai_cost_tracker.db.models import AuditLog


async def test_record_adds_row_in_callers_transaction(session, tenant_id):
    who = SimpleNamespace(oid="u-1", name="Ada")
    audit.record(session, tenant_id, who, "budget.create", target_type="budget", target_id="b1")
    await session.rollback()
    assert (await session.scalars(select(AuditLog))).all() == []

    audit.record(session, tenant_id, who, "budget.create", details={"amount": "100"})
    await session.commit()
    row = await session.scalar(select(AuditLog))
    assert (row.actor_id, row.actor_name, row.action) == ("u-1", "Ada", "budget.create")
