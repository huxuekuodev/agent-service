"""健康检查。"""

from fastapi import APIRouter

from app.core.response import ok

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return ok({"status": "ok"})
