import os
import tempfile
from typing import Annotated
from uuid import UUID, uuid4

from api_logging import get_logger
from budgets import BudgetBlock, BudgetExceeded
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from investment_filings import get_filing_source_status
from investment_service import (
    MAX_DOCUMENT_BYTES,
    AnalysisInProgress,
    enqueue_investment_analysis,
)
from investment_service import analyze_document as analyze_investment_document
from investment_service import get_analysis as get_investment_analysis
from investment_service import get_dashboard as get_investment_dashboard
from investment_service import store_document_path as store_investment_document_path
from investment_service import store_document_url as store_investment_document_url
from jobs import accept_and_enqueue_operation
from pydantic import BaseModel, ConfigDict, Field
from run_lifecycle import RunAcceptanceConflict

from config import load_config
from contracts import InvestmentUrlIngestRequest

router = APIRouter(prefix="/investment", tags=["investment"])
logger = get_logger("api.investment")


def _reject_declared_oversize(request: Request) -> None:
    declared = request.headers.get("content-length")
    if not declared:
        return
    try:
        declared_size = int(declared)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Content-Length")
    if declared_size > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="Document exceeds 20 MB")


def _wants_analysis(metadata: dict | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    value = metadata.get("analyze")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _accept_and_enqueue(
    config: dict,
    correlation_id: str,
    run_kind: str,
    requested_component: str | None,
    *,
    idempotency_key: str | None = None,
    request_summary: dict | None = None,
    payload: dict | None = None,
    dedupe_key: str | None = None,
    input_fingerprint: str | None = None,
    priority: int = 100,
    max_attempts: int = 3,
    triggered_by: str = "api",
):
    try:
        return accept_and_enqueue_operation(
            config,
            correlation_id=correlation_id,
            triggered_by=triggered_by,
            run_kind=run_kind,
            requested_component=requested_component,
            idempotency_key=idempotency_key,
            request_summary=request_summary,
            dedupe_key=dedupe_key,
            input_fingerprint=input_fingerprint,
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
        )
    except RunAcceptanceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "run_acceptance_failed",
            correlation_id=correlation_id,
            run_kind=run_kind,
            error=str(exc),
        )
        raise HTTPException(
            status_code=503, detail="Run acceptance unavailable"
        ) from exc


@router.get("/dashboard")
async def investment_dashboard():
    config = load_config()
    return get_investment_dashboard(config)


@router.get("/analyses/{analysis_id}")
async def investment_analysis(analysis_id: UUID):
    config = load_config()
    payload = get_investment_analysis(config, str(analysis_id))
    if payload is None:
        raise HTTPException(status_code=404, detail="Investment analysis not found")
    return payload


@router.post("/documents", status_code=201)
async def ingest_investment_document(request: Request):
    """Stream the inbound upload to temp storage, then store and optionally analyze.

    The declared Content-Length is rejected early; the running chunk total is
    capped while streaming (so chunked bodies cannot bypass the cap), and the
    spool file is always removed, success or failure.
    """
    _reject_declared_oversize(request)
    metadata = dict(request.query_params)
    spool_path: str | None = None
    try:
        spool = tempfile.NamedTemporaryFile(
            prefix="investment-upload-", suffix=".bin", delete=False
        )
        spool_path = spool.name
        try:
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_DOCUMENT_BYTES:
                    raise HTTPException(
                        status_code=413, detail="Document exceeds 20 MB"
                    )
                spool.write(chunk)
            spool.flush()
        finally:
            spool.close()

        config = load_config()
        try:
            result = store_investment_document_path(
                config,
                metadata,
                spool_path,
                request.headers.get("content-type"),
                extract=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(
                "investment_document_ingest_failed",
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="Investment document storage unavailable",
            ) from exc
    finally:
        if spool_path is not None:
            try:
                os.unlink(spool_path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning(
                    "investment_upload_spool_cleanup_failed",
                    path=spool_path,
                )

    if _wants_analysis(metadata):
        try:
            result = {
                **result,
                "analysis": enqueue_investment_analysis(
                    config,
                    str(result["document_id"]),
                ),
            }
        except Exception as exc:
            logger.error(
                "investment_analysis_enqueue_failed",
                document_id=str(result.get("document_id") or ""),
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="Investment analysis could not be scheduled",
            ) from exc

    return JSONResponse(status_code=201, content=result)


@router.post("/urls", status_code=201)
async def ingest_investment_url(
    body: InvestmentUrlIngestRequest = Body(...),
):
    config = load_config()
    payload = body.model_dump(exclude_none=True)
    try:
        result = store_investment_document_url(config, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "investment_url_ingest_failed",
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="Investment document could not be fetched",
        ) from exc
    if _wants_analysis(payload):
        try:
            result = {
                **result,
                "analysis": enqueue_investment_analysis(
                    config,
                    str(result["document_id"]),
                ),
            }
        except Exception as exc:
            logger.error(
                "investment_analysis_enqueue_failed",
                document_id=str(result.get("document_id") or ""),
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="Investment analysis could not be scheduled",
            ) from exc
    return JSONResponse(status_code=201, content=result)


@router.post("/documents/{document_id}/analyze")
async def run_investment_analysis(
    document_id: UUID,
    body: dict | None = Body(default=None),
):
    market_inputs = body.get("market_inputs") if isinstance(body, dict) else None
    if market_inputs is not None and not isinstance(market_inputs, dict):
        raise HTTPException(status_code=422, detail="market_inputs must be an object")
    config = load_config()
    try:
        return analyze_investment_document(
            config,
            str(document_id),
            market_inputs,
        )
    except BudgetBlock as exc:
        status_code = 429 if isinstance(exc, BudgetExceeded) else 503
        raise HTTPException(status_code=status_code, detail=exc.safe_reason) from exc
    except AnalysisInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "investment_analysis_failed",
            document_id=str(document_id),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Investment analysis failed",
        ) from exc


@router.get("/filings/status")
async def investment_filings_status():
    config = load_config()
    return get_filing_source_status(config)


class _StrictRequest(BaseModel):
    """Strict durable-acceptance bodies: no coercion, no unknown fields."""

    model_config = ConfigDict(extra="forbid")


class FilingsRequest(_StrictRequest):
    correlation_id: UUID | None = None
    idempotency_key: Annotated[
        str | None, Field(min_length=1, max_length=128, strict=True)
    ] = None
    auto_analyze: Annotated[bool, Field(strict=True)] = False


@router.post("/filings/collect", status_code=202)
async def trigger_filing_collection(
    body: FilingsRequest | None = Body(default=None),
):
    request = body if body is not None else FilingsRequest()
    correlation_id = (
        str(request.correlation_id) if request.correlation_id else str(uuid4())
    )
    config = load_config()
    accepted_at, _ = _accept_and_enqueue(
        config,
        correlation_id,
        "filings",
        "investment_filings",
        idempotency_key=request.idempotency_key,
        request_summary={"auto_analyze": request.auto_analyze},
        payload={"auto_analyze": request.auto_analyze},
        max_attempts=3,
    )
    job_id = correlation_id
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "accepted_at": accepted_at.isoformat()},
    )
