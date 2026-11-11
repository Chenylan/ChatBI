from fastapi import APIRouter
from backend.api.chat import router as chat_router
from backend.api.metrics import router as metrics_router

router = APIRouter()
router.include_router(chat_router, prefix="/chat", tags=["chat"])
router.include_router(metrics_router, prefix="/metrics", tags=["metrics"])

