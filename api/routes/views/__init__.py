from fastapi import APIRouter

from routes.views.dashboard import router as dashboard_router
from routes.views.logs import router as logs_router
from routes.views.quality import router as quality_router
from routes.views.operations import router as operations_router
from routes.views.news import router as news_router
from routes.views.settings import router as settings_router
from routes.views.assets import router as assets_router
from routes.views.setup import router as setup_router

router = APIRouter()

router.include_router(dashboard_router)
router.include_router(logs_router)
router.include_router(quality_router)
router.include_router(operations_router)
router.include_router(news_router)
router.include_router(settings_router)
router.include_router(assets_router)
router.include_router(setup_router)
