from fastapi import APIRouter

from routes.views.dashboard import router as dashboard_router
from routes.views.logs import router as logs_router
from routes.views.quality import router as quality_router

router = APIRouter()

router.include_router(dashboard_router)
router.include_router(logs_router)
router.include_router(quality_router)
