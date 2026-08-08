from fastapi import APIRouter

from routes.views.assets import router as assets_router
from routes.views.cockpit_panels import router as cockpit_panels_router
from routes.views.dashboard import router as dashboard_router
from routes.views.investment import router as investment_router
from routes.views.logs import router as logs_router
from routes.views.news import router as news_router
from routes.views.operations import router as operations_router
from routes.views.quality import router as quality_router
from routes.views.research import router as research_router
from routes.views.settings import router as settings_router
from routes.views.setup import router as setup_router
from routes.views.since_last_view import router as since_last_view_router
from routes.views.watchlist_grid import router as watchlist_grid_router

router = APIRouter()

router.include_router(dashboard_router)
router.include_router(logs_router)
router.include_router(quality_router)
router.include_router(research_router)
router.include_router(operations_router)
router.include_router(news_router)
router.include_router(settings_router)
router.include_router(assets_router)
router.include_router(setup_router)
router.include_router(since_last_view_router)
router.include_router(watchlist_grid_router)
router.include_router(cockpit_panels_router)
router.include_router(investment_router)
