from fastapi import APIRouter

from routes.json.assets import router as assets_router
from routes.json.atoms import router as atoms_router
from routes.json.briefing import router as briefing_router
from routes.json.events import router as events_router
from routes.json.evidence import router as evidence_router
from routes.json.investment import router as investment_router
from routes.json.macro import router as macro_router
from routes.json.news import router as news_router
from routes.json.opinions import router as opinions_router
from routes.json.regime import router as regime_router
from routes.json.research import router as research_router
from routes.json.settings import router as settings_router
from routes.json.setup import router as setup_router
from routes.json.system import router as system_router
from routes.json.triggers import router as triggers_router
from routes.json.watchlist import router as watchlist_router

router = APIRouter(prefix="/api")

router.include_router(atoms_router)
router.include_router(briefing_router)
router.include_router(regime_router)
router.include_router(opinions_router)
router.include_router(events_router)
router.include_router(macro_router)
router.include_router(watchlist_router)
router.include_router(system_router)
router.include_router(triggers_router)
router.include_router(evidence_router)
router.include_router(news_router)
router.include_router(settings_router)
router.include_router(assets_router)
router.include_router(setup_router)
router.include_router(investment_router)
router.include_router(research_router)
