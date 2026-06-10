import asyncio
import json

import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from config import load_config

router = APIRouter()
ORCHESTRATOR_URL = "http://orchestrator:8000"


@router.get("/watchlist")
def get_watchlist():
    config = load_config()
    watchlist = config.get("watchlist", {}).get("trading", [])
    return {"instruments": watchlist}


@router.get("/quotes")
async def get_quotes():
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{ORCHESTRATOR_URL}/quotes")
        response.raise_for_status()
        return response.json()


@router.get("/quotes/stream")
async def stream_quotes():
    async def events():
        while True:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(f"{ORCHESTRATOR_URL}/quotes")
                    response.raise_for_status()
                    yield f"data: {json.dumps(response.json())}\n\n"
            except Exception as exc:
                yield f"event: stream-status\ndata: {json.dumps({'status': 'unavailable', 'error': str(exc)})}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(events(), media_type="text/event-stream")
