import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from auth import mint_sse_token, verify_sse_token

from config import load_config

router = APIRouter()
ORCHESTRATOR_URL = "http://orchestrator:8000"


@router.get("/watchlist")
def get_watchlist():
    config = load_config()
    watchlist = config.get("watchlist", {}).get("trading", [])
    return {"instruments": watchlist}


@router.get("/quotes")
async def get_quotes(request: Request):
    response = await request.app.state.orchestrator_client.get(
        f"{ORCHESTRATOR_URL}/quotes", timeout=5.0,
    )
    response.raise_for_status()
    return response.json()


async def _quote_events(request: Request, sleep=asyncio.sleep):
    while not await request.is_disconnected():
        try:
            response = await request.app.state.orchestrator_client.get(
                f"{ORCHESTRATOR_URL}/quotes", timeout=5.0,
            )
            response.raise_for_status()
            yield f"data: {json.dumps(response.json())}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception:
            yield f"event: stream-status\ndata: {json.dumps({'status': 'unavailable', 'error': 'upstream unavailable'})}\n\n"
        if await request.is_disconnected():
            break
        await sleep(2)


@router.get("/quotes/stream")
async def stream_quotes(request: Request, token: str | None = None):
    import time
    now = int(time.time())
    used = {key: expiry for key, expiry in getattr(request.app.state, "used_sse_tokens", {}).items() if expiry > now}
    request.app.state.used_sse_tokens = used
    if not verify_sse_token(token, "/api/quotes/stream", used):
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Basic"})
    return StreamingResponse(_quote_events(request), media_type="text/event-stream")


@router.get("/quotes/stream-token")
def stream_token():
    return {"token": mint_sse_token()}
