"""HTTP routes; thin adapters over ``EnrichmentService`` and the repository."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse

from src.api.schemas import (
    EnrichRequest,
    EnrichResponse,
    FitTier,
    HealthResponse,
    LeadListResponse,
    LeadResponse,
    LeadStatus,
)
from src.api.security import (
    HostResolver,
    UnsafeUrlError,
    assert_public_host,
    resolve_host_addresses,
)
from src.services.enrichment import EnrichmentService
from src.storage.base import BaseLeadRepository

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_PAGE_SIZE = 100


def get_service(request: Request) -> EnrichmentService:
    service: EnrichmentService = request.app.state.service
    return service


def get_repo(request: Request) -> BaseLeadRepository:
    repo: BaseLeadRepository = request.app.state.repo
    return repo


def get_host_resolver() -> HostResolver:
    """Overridable in tests so URL validation never touches real DNS."""
    return resolve_host_addresses


ServiceDep = Annotated[EnrichmentService, Depends(get_service)]
RepoDep = Annotated[BaseLeadRepository, Depends(get_repo)]
ResolverDep = Annotated[HostResolver, Depends(get_host_resolver)]


@router.post(
    "/api/v1/enrich",
    response_model=EnrichResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"model": EnrichResponse, "description": "Already processed"}},
    tags=["enrichment"],
)
async def enrich(
    body: EnrichRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    service: ServiceDep,
    resolver: ResolverDep,
) -> EnrichResponse:
    """Queue a URL for enrichment and return immediately.

    Poll ``GET /api/v1/leads/{lead_id}`` for the outcome.  A URL that is already
    processed is not re-run unless ``force`` is set; it is reported with 200.
    """
    try:
        await assert_public_host(body.url, resolver)
    except UnsafeUrlError as exc:
        raise HTTPException(422, detail=str(exc)) from exc

    submission = await service.submit(body.url, force=body.force)
    if not submission.needs_processing:
        response.status_code = status.HTTP_200_OK
        return EnrichResponse(
            lead_id=submission.lead_id,
            url=submission.url,
            status=submission.status,
            message="URL already processed; pass force=true to re-enrich.",
        )

    background_tasks.add_task(service.process_lead, submission.lead_id, submission.url)
    return EnrichResponse(
        lead_id=submission.lead_id,
        url=submission.url,
        status=submission.status,
        message="Enrichment queued.",
    )


@router.get("/api/v1/leads/{lead_id}", response_model=LeadResponse, tags=["leads"])
async def get_lead(lead_id: int, repo: RepoDep) -> LeadResponse:
    row = await repo.get_lead_by_id(lead_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Lead {lead_id} not found"
        )
    return LeadResponse.model_validate(row)


@router.get("/api/v1/leads", response_model=LeadListResponse, tags=["leads"])
async def list_leads(
    repo: RepoDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)] = 50,
    fit_tier: FitTier | None = None,
    status_filter: Annotated[LeadStatus | None, Query(alias="status")] = None,
) -> LeadListResponse:
    rows, total = await repo.list_leads(
        skip=(page - 1) * page_size,
        limit=page_size,
        fit_tier=fit_tier,
        status=status_filter,
    )
    return LeadListResponse(
        items=[LeadResponse.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse}},
    tags=["ops"],
)
async def health(repo: RepoDep) -> HealthResponse | JSONResponse:
    try:
        reachable = await repo.ping()
    except Exception:
        # A failing probe must report unhealthy, not surface as a 500.
        logger.exception("Health check failed: database unreachable")
        reachable = False

    if reachable:
        return HealthResponse(status="healthy", database="ok")
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=HealthResponse(status="unhealthy", database="unreachable").model_dump(),
    )
