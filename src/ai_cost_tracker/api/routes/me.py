from typing import Annotated

from fastapi import APIRouter, Depends

from ai_cost_tracker.api.auth import Principal, get_principal

router = APIRouter(tags=["me"])


@router.get("/me")
async def me(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    return principal
