from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness only: no auth, no database, so probes don't depend on Entra or Postgres."""
    return {"status": "ok"}
