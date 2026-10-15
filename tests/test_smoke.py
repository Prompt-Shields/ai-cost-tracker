from sqlalchemy import select

from ai_cost_tracker.db.models import Tenant


async def test_schema_and_tenant_fixture(session, tenant_id):
    tenant = (await session.execute(select(Tenant))).scalar_one()
    assert tenant.id == tenant_id
