from fastapi import APIRouter, Request
from routes.json.assets import get_asset

router = APIRouter()

@router.get("/assets/{symbol}")
def asset_page(request: Request, symbol: str):
    return request.app.state.templates.TemplateResponse(
        request, "asset.html", {"request": request, **get_asset(symbol)}
    )
