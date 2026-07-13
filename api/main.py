from collections.abc import Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import verify_credentials
from config import load_config
from logging_config import setup_logging
from routes.json import router as json_router
from routes.views import router as views_router


def create_app(
    orchestrator_client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> FastAPI:
    config = load_config()
    log_level = config.get("logging", {}).get("level", "INFO")
    setup_logging(level=log_level)

    client_factory = orchestrator_client_factory or httpx.AsyncClient

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.orchestrator_client = client_factory(
            timeout=httpx.Timeout(connect=2.0, read=10.0, write=10.0, pool=2.0),
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
        )
        try:
            yield
        finally:
            await app.state.orchestrator_client.aclose()

    app = FastAPI(
        title="Trading Data API",
        version="0.1.0",
        dependencies=[Depends(verify_credentials)],
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.state.templates = Jinja2Templates(directory="templates")

    app.include_router(json_router)
    app.include_router(views_router)

    return app


app = create_app()