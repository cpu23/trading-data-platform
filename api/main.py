from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import verify_credentials
from config import load_config
from logging_config import setup_logging
from routes.json import router as json_router
from routes.views import router as views_router


def create_app() -> FastAPI:
    config = load_config()
    log_level = config.get("logging", {}).get("level", "INFO")
    setup_logging(level=log_level)

    app = FastAPI(
        title="Trading Data API",
        version="0.1.0",
        dependencies=[Depends(verify_credentials)],
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