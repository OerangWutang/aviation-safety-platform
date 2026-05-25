from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from atlas.config import get_settings
from atlas.domain.causality.exceptions import (
    HfacsAttributionConflictError,
    HfacsAttributionNotFoundError,
    HfacsCategoryNotFoundError,
    HfacsSubcategoryNotFoundError,
    SheloFactorConflictError,
    SheloFactorInteractionConflictError,
    SheloFactorInteractionSameNodeError,
    SheloFactorNotFoundError,
)
from atlas.domain.cms.exceptions import (
    ChangelogEntryRetractedError,
    CmsContentModifiedError,
    GlossaryTermRetractedError,
    MethodologyPageRetractedError,
)
from atlas.domain.exceptions import (
    ArgusSignalModifiedError,
    AtlasError,
    CannotMergeIntoSelfError,
    ClaimNotInConflictError,
    ConcurrentUpsertError,
    ConflictAlreadyResolvedError,
    ConflictModifiedError,
    ConflictReconciliationError,
    DomainValidationError,
    EventAlreadyMergedError,
    IdempotencyKeyPayloadMismatchError,
    IngestionInProgressError,
    InvariantViolationError,
    MappingError,
    NotFoundError,
    PersistenceCorruptionError,
    ReviewAlreadyResolvedError,
    SourceRecordEventMismatchError,
)
from atlas.domain.nl_search.exceptions import SavedNlQueryNotFoundError
from atlas.domain.publication.exceptions import (
    PublicEventPageModifiedError,
    PublicEventPageNotPublishedError,
    PublicEventPageRetractedError,
)
from atlas.domain.services.ingestion import NormalizationError
from atlas.domain.tenancy.exceptions import (
    CrossTenantAccessError,
    DeidentificationRequiredError,
    NotATenantApiKeyError,
    TenantClaimBatchTooLargeError,
    TenantClaimUnknownEventError,
    TenantInactiveError,
    TenantIngestionRunClosedError,
    TenantIngestionRunNotFoundError,
    TenantSourceNotFoundError,
)
from atlas.infrastructure.db.orm_models import SourceModel
from atlas.infrastructure.db.session import async_session_factory, get_engine
from atlas.logging_config import setup_logging
from atlas.presentation.api.metrics import install_prometheus
from atlas.presentation.api.middleware import (
    InMemoryRateLimitMiddleware,
    RequestBodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from atlas.presentation.api.routers import (
    accidents,
    admin,
    argus,
    audit,
    causality,
    chronos,
    cms,
    conflicts,
    editorial,
    hermes,
    ingestion,
    maps,
    metering,
    nl_search,
    orion,
    provenance,
    public,
    search,
    tenancy,
)
from atlas.presentation.api.schemas.errors import ErrorDetail, ErrorEnvelope

if TYPE_CHECKING:
    from atlas.config import Settings

setup_logging()
logger = logging.getLogger(__name__)


async def assert_curator_override_source() -> None:
    """Fail fast if the CuratorOverride seed source required for manual overrides is missing."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(SourceModel).where(SourceModel.id == get_settings().curator_override_source_id)
        )
        if result.scalar_one_or_none() is None:
            raise RuntimeError(
                "Required seed row 'CuratorOverride' is missing. "
                "Run 'alembic upgrade head' before starting the API."
            )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_settings().validate_api_runtime_settings()
    await assert_curator_override_source()
    yield


def _err(
    code: str,
    message: str,
    status: int,
    details: dict[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=jsonable_encoder(
            ErrorEnvelope(
                error=ErrorDetail(code=code, message=message, details=details or {})
            ).model_dump()
        ),
        headers=headers,
    )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Atlas Backend",
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.effective_api_docs_enabled else None,
        redoc_url="/redoc" if settings.effective_api_docs_enabled else None,
        openapi_url="/openapi.json" if settings.effective_api_docs_enabled else None,
    )
    _add_middleware(app, settings)
    _include_routers(app)
    _register_exception_handlers(app)
    _add_utility_routes(app)
    return app


def _add_middleware(app: FastAPI, settings: Settings) -> None:
    """Register middleware in the correct order.

    Middleware is applied in reverse registration order (LIFO), so the last
    one added here is the outermost layer that wraps all other middleware and
    handler responses.  Security headers are therefore last (outermost) so
    they wrap even early-exit middleware responses like rate-limit rejections.
    """
    if settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    rate_limit_requests = settings.effective_rate_limit_requests
    if rate_limit_requests > 0:
        app.add_middleware(
            InMemoryRateLimitMiddleware,
            requests=rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
        )
    app.add_middleware(
        RequestBodySizeLimitMiddleware,
        max_bytes=settings.max_raw_payload_bytes + settings.request_body_overhead_bytes,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["DELETE", "GET", "OPTIONS", "POST", "PUT"],
            allow_headers=["Content-Type", "X-API-Key", "Authorization"],
        )

    if settings.security_headers_enabled:
        app.add_middleware(
            SecurityHeadersMiddleware,
            hsts_enabled=settings.hsts_enabled,
            hsts_max_age_seconds=settings.hsts_max_age_seconds,
        )


def _include_routers(app: FastAPI) -> None:
    """Wire all domain routers and install the Prometheus metrics endpoint."""
    app.include_router(accidents.router, prefix="/api/v1")
    app.include_router(conflicts.router, prefix="/api/v1")
    app.include_router(ingestion.router, prefix="/api/v1")
    app.include_router(provenance.router, prefix="/api/v1")
    app.include_router(orion.router, prefix="/api/v1")
    app.include_router(chronos.router, prefix="/api/v1")
    app.include_router(hermes.router, prefix="/api/v1")
    app.include_router(argus.router, prefix="/api/v1")
    app.include_router(public.router, prefix="/api/v1")
    app.include_router(editorial.router, prefix="/api/v1")
    app.include_router(search.router, prefix="/api/v1")
    app.include_router(audit.router, prefix="/api/v1")
    app.include_router(maps.router, prefix="/api/v1")
    app.include_router(nl_search.router, prefix="/api/v1")
    app.include_router(metering.tenant_router, prefix="/api/v1")
    app.include_router(metering.admin_router, prefix="/api/v1")
    app.include_router(cms.public_router, prefix="/api/v1")
    app.include_router(cms.editorial_router, prefix="/api/v1")
    app.include_router(causality.public_router, prefix="/api/v1")
    app.include_router(causality.editorial_router, prefix="/api/v1")
    app.include_router(tenancy.router, prefix="/api/v1")
    app.include_router(admin.router, prefix="/api/v1/admin")
    install_prometheus(app)


def _register_exception_handlers(app: FastAPI) -> None:
    """Register all domain-exception-to-HTTP-response mappings.

    Each handler is a thin adapter that calls ``_err`` with the right HTTP
    status code.  The mapping is deliberately exhaustive: any domain error
    that escapes to an un-handled ``RuntimeError`` or ``ValueError`` is
    treated as a programmer bug and returns 500 with a generic message.
    """

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Strip the Pydantic v2 ``url`` field from each error dict.
        # RequestValidationError pre-builds its errors as plain dicts and
        # does not forward ``include_url`` to pydantic, so we clean them here.
        # Exposing ``https://errors.pydantic.dev/…`` URLs in API responses
        # leaks library-version detail and is noise for API consumers.
        errors = [{k: v for k, v in err.items() if k != "url"} for err in exc.errors()]
        return _err("VALIDATION_ERROR", "Request validation failed", 422, {"errors": errors})

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        details: dict[str, object]
        if isinstance(detail, dict):
            code = str(detail.get("code") or f"HTTP_{exc.status_code}")
            message = str(detail.get("detail") or detail.get("message") or "Request failed")
            details = {
                str(k): v for k, v in detail.items() if k not in {"code", "detail", "message"}
            }
        else:
            code = f"HTTP_{exc.status_code}"
            message = str(detail)
            details = {}
        return _err(code, message, exc.status_code, details, headers=exc.headers)

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
        return _err(exc.code, exc.message, 404)

    @app.exception_handler(ConflictAlreadyResolvedError)
    async def conflict_resolved_handler(
        request: Request, exc: ConflictAlreadyResolvedError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 409)

    @app.exception_handler(ClaimNotInConflictError)
    async def claim_not_in_conflict_handler(
        request: Request, exc: ClaimNotInConflictError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 422)

    @app.exception_handler(ConflictModifiedError)
    async def conflict_modified_handler(
        request: Request, exc: ConflictModifiedError
    ) -> JSONResponse:
        return _err(
            exc.code,
            "Conflict has been modified since you loaded it.",
            409,
            {
                "conflict_id": str(exc.conflict_id),
                "current_version": exc.current_version,
                "current_conflict": exc.current_conflict,
                "current_projection": exc.current_projection,
                "latest_activity": exc.latest_activity,
                "modifier_reason": exc.modifier_reason,
            },
        )

    @app.exception_handler(ArgusSignalModifiedError)
    async def argus_signal_modified_handler(
        request: Request, exc: ArgusSignalModifiedError
    ) -> JSONResponse:
        return _err(
            exc.code,
            "Argus signal has been modified since you loaded it.",
            409,
            {
                "signal_id": str(exc.signal_id),
                "current_version": exc.current_version,
                "current_signal": exc.current_signal,
            },
        )

    @app.exception_handler(ConcurrentUpsertError)
    async def concurrent_upsert_handler(
        request: Request, exc: ConcurrentUpsertError
    ) -> JSONResponse:
        # The error's own docstring says callers should retry once; signal that.
        return _err(exc.code, exc.message, 503, headers={"Retry-After": "1"})

    @app.exception_handler(ConflictReconciliationError)
    async def conflict_reconciliation_handler(
        request: Request, exc: ConflictReconciliationError
    ) -> JSONResponse:
        logger.error(
            "Conflict reconciliation failed on %s for conflict=%s op=%s retries=%d",
            request.url.path,
            exc.conflict_id,
            exc.operation,
            exc.retries,
        )
        return _err(exc.code, exc.message, 503, headers={"Retry-After": "5"})

    @app.exception_handler(CannotMergeIntoSelfError)
    async def cannot_merge_into_self_handler(
        request: Request, exc: CannotMergeIntoSelfError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 422)

    @app.exception_handler(ReviewAlreadyResolvedError)
    async def review_already_resolved_handler(
        request: Request, exc: ReviewAlreadyResolvedError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 409)

    @app.exception_handler(NormalizationError)
    async def normalization_error_handler(
        request: Request, exc: NormalizationError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 422)

    @app.exception_handler(DomainValidationError)
    async def domain_validation_handler(
        request: Request, exc: DomainValidationError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 422)

    @app.exception_handler(IngestionInProgressError)
    async def ingestion_in_progress_handler(
        request: Request, exc: IngestionInProgressError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 409, headers={"Retry-After": "2"})

    @app.exception_handler(IdempotencyKeyPayloadMismatchError)
    async def idempotency_key_payload_mismatch_handler(
        request: Request, exc: IdempotencyKeyPayloadMismatchError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 409)

    @app.exception_handler(EventAlreadyMergedError)
    async def event_already_merged_handler(
        request: Request, exc: EventAlreadyMergedError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 409)

    @app.exception_handler(SourceRecordEventMismatchError)
    async def source_record_event_mismatch_handler(
        request: Request, exc: SourceRecordEventMismatchError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 409)

    @app.exception_handler(PersistenceCorruptionError)
    async def persistence_corruption_handler(
        request: Request, exc: PersistenceCorruptionError
    ) -> JSONResponse:
        logger.exception("Persistence corruption on %s: %s", request.url.path, exc.message)
        return _err("INTERNAL_ERROR", "An unexpected internal error occurred.", 500)

    @app.exception_handler(InvariantViolationError)
    async def invariant_violation_handler(
        request: Request, exc: InvariantViolationError
    ) -> JSONResponse:
        logger.exception("Invariant violation on %s: %s", request.url.path, exc.message)
        return _err("INTERNAL_ERROR", "An unexpected internal error occurred.", 500)

    @app.exception_handler(MappingError)
    async def mapping_error_handler(request: Request, exc: MappingError) -> JSONResponse:
        logger.exception("ORM mapping error on %s", request.url.path)
        return _err("INTERNAL_ERROR", "An unexpected error occurred.", 500)

    @app.exception_handler(PublicEventPageModifiedError)
    async def public_event_page_modified_handler(
        request: Request, exc: PublicEventPageModifiedError
    ) -> JSONResponse:
        # 409 Conflict — mirrors the ConflictModifiedError convention.
        # The body carries the actual_version so callers can decide
        # whether to refetch and retry or surface the mismatch to the
        # editor.
        return _err(
            exc.code,
            "Public event page was modified by another writer.",
            409,
            {
                "expected_version": exc.expected_version,
                "actual_version": exc.actual_version,
            },
        )

    @app.exception_handler(PublicEventPageNotPublishedError)
    async def public_event_page_not_published_handler(
        request: Request, exc: PublicEventPageNotPublishedError
    ) -> JSONResponse:
        # 404 (not 403/410) so DRAFT existence is not observable to
        # anonymous-style callers.  The use case raises this only when
        # the row exists but is DRAFT; pure misses raise
        # ``PublicEventPageNotFoundError`` which is handled by the
        # generic ``NotFoundError`` 404 above.
        return _err(exc.code, "Public event page not found", 404)

    @app.exception_handler(PublicEventPageRetractedError)
    async def public_event_page_retracted_handler(
        request: Request, exc: PublicEventPageRetractedError
    ) -> JSONResponse:
        # 410 Gone — the URL was once valid; consumers should treat
        # it as a permanent removal and update their references.  The
        # retraction note is carried in the details payload so a UI
        # can render a meaningful notice without prose-scraping.
        return _err(
            exc.code,
            exc.message,
            410,
            {"slug": exc.slug, "retraction_note": exc.retraction_note},
        )

    @app.exception_handler(CrossTenantAccessError)
    async def cross_tenant_access_handler(
        request: Request, exc: CrossTenantAccessError
    ) -> JSONResponse:
        # 403 — the caller is authenticated but is targeting a tenant
        # they aren't bound to.  Audit-loggable: this is the signal we
        # want highlighted in security review.  Response body does NOT
        # include the target tenant's display_name; only the bare ids
        # already known to the caller.
        logger.warning(
            "Cross-tenant access attempted: caller_tenant=%s target=%s path=%s",
            exc.caller_tenant_id,
            exc.target_tenant_id,
            request.url.path,
        )
        return _err(exc.code, exc.message, 403)

    @app.exception_handler(TenantInactiveError)
    async def tenant_inactive_handler(request: Request, exc: TenantInactiveError) -> JSONResponse:
        return _err(exc.code, exc.message, 403)

    @app.exception_handler(NotATenantApiKeyError)
    async def not_a_tenant_api_key_handler(
        request: Request, exc: NotATenantApiKeyError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 403)

    @app.exception_handler(TenantIngestionRunClosedError)
    async def tenant_ingestion_run_closed_handler(
        request: Request, exc: TenantIngestionRunClosedError
    ) -> JSONResponse:
        # 409: the run exists and the request is well-formed; the
        # state is the conflict.
        return _err(exc.code, exc.message, 409)

    @app.exception_handler(TenantIngestionRunNotFoundError)
    async def tenant_ingestion_run_not_found_handler(
        request: Request, exc: TenantIngestionRunNotFoundError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 404)

    @app.exception_handler(TenantSourceNotFoundError)
    async def tenant_source_not_found_handler(
        request: Request, exc: TenantSourceNotFoundError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 404)

    @app.exception_handler(TenantClaimBatchTooLargeError)
    async def tenant_claim_batch_too_large_handler(
        request: Request, exc: TenantClaimBatchTooLargeError
    ) -> JSONResponse:
        # 422: the request body is the problem (the batch is too big).
        # The existing generic DomainValidationError handler would
        # also produce 422; we use a specific handler to keep the
        # error code stable across catch paths.
        return _err(exc.code, exc.message, 422)

    @app.exception_handler(TenantClaimUnknownEventError)
    async def tenant_claim_unknown_event_handler(
        request: Request, exc: TenantClaimUnknownEventError
    ) -> JSONResponse:
        # 422: the submitted event_ids don't exist in the public corpus.
        # Return the offending IDs so the operator can identify which
        # claims need correction without re-examining the whole batch.
        return _err(
            exc.code,
            exc.message,
            422,
            {"unknown_event_ids": [str(i) for i in sorted(exc.unknown_ids, key=str)]},
        )

    @app.exception_handler(DeidentificationRequiredError)
    async def deidentification_required_handler(
        request: Request, exc: DeidentificationRequiredError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 422)

    # ── Phase 4: causality ────────────────────────────────────────────
    #
    # Mostly 404 / 409 / 422 mirroring the patterns established in
    # earlier phases.  No retraction handlers here because Phase 4
    # entities inherit visibility from PublicEventPage — the Phase 1
    # retraction handler already covers the 410 path.

    @app.exception_handler(HfacsCategoryNotFoundError)
    async def hfacs_category_not_found_handler(
        request: Request, exc: HfacsCategoryNotFoundError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 404)

    @app.exception_handler(HfacsSubcategoryNotFoundError)
    async def hfacs_subcategory_not_found_handler(
        request: Request, exc: HfacsSubcategoryNotFoundError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 404)

    @app.exception_handler(HfacsAttributionNotFoundError)
    async def hfacs_attribution_not_found_handler(
        request: Request, exc: HfacsAttributionNotFoundError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 404)

    @app.exception_handler(HfacsAttributionConflictError)
    async def hfacs_attribution_conflict_handler(
        request: Request, exc: HfacsAttributionConflictError
    ) -> JSONResponse:
        # 409 — duplicate natural key OR stale expected_version.
        # The caller's UI re-fetches and retries either way.
        return _err(exc.code, exc.message, 409)

    @app.exception_handler(SheloFactorNotFoundError)
    async def shelo_factor_not_found_handler(
        request: Request, exc: SheloFactorNotFoundError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 404)

    @app.exception_handler(SheloFactorConflictError)
    async def shelo_factor_conflict_handler(
        request: Request, exc: SheloFactorConflictError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 409)

    @app.exception_handler(SheloFactorInteractionConflictError)
    async def shelo_factor_interaction_conflict_handler(
        request: Request, exc: SheloFactorInteractionConflictError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 409)

    @app.exception_handler(SheloFactorInteractionSameNodeError)
    async def shelo_factor_interaction_same_node_handler(
        request: Request, exc: SheloFactorInteractionSameNodeError
    ) -> JSONResponse:
        return _err(exc.code, exc.message, 422)

    @app.exception_handler(SavedNlQueryNotFoundError)
    async def saved_nl_query_not_found_handler(
        request: Request, exc: SavedNlQueryNotFoundError
    ) -> JSONResponse:
        # 404 — cross-user delete intentionally returns 404 rather
        # than 403 so the existence of another user's saved query
        # doesn't leak.
        return _err(exc.code, exc.message, 404)

    @app.exception_handler(CmsContentModifiedError)
    async def cms_content_modified_handler(
        request: Request, exc: CmsContentModifiedError
    ) -> JSONResponse:
        # 409 Conflict — same shape as Phase 9's PublicEventPageModifiedError.
        # Carries the expected/actual versions and the entity kind so a UI
        # can re-fetch, surface a friendly conflict message, and retry.
        return _err(
            exc.code,
            exc.message,
            409,
            {
                "kind": exc.kind,
                "entity_id": str(exc.entity_id),
                "expected_version": exc.expected_version,
                "actual_version": exc.actual_version,
            },
        )

    @app.exception_handler(GlossaryTermRetractedError)
    async def glossary_term_retracted_handler(
        request: Request, exc: GlossaryTermRetractedError
    ) -> JSONResponse:
        # 410 Gone — same contract as Phase 9 page retraction.
        return _err(
            exc.code,
            exc.message,
            410,
            {"term": exc.term, "retraction_note": exc.retraction_note},
        )

    @app.exception_handler(MethodologyPageRetractedError)
    async def methodology_page_retracted_handler(
        request: Request, exc: MethodologyPageRetractedError
    ) -> JSONResponse:
        return _err(
            exc.code,
            exc.message,
            410,
            {"slug": exc.slug, "retraction_note": exc.retraction_note},
        )

    @app.exception_handler(ChangelogEntryRetractedError)
    async def changelog_entry_retracted_handler(
        request: Request, exc: ChangelogEntryRetractedError
    ) -> JSONResponse:
        return _err(
            exc.code,
            exc.message,
            410,
            {"slug": exc.slug, "retraction_note": exc.retraction_note},
        )

    @app.exception_handler(AtlasError)
    async def atlas_error_handler(request: Request, exc: AtlasError) -> JSONResponse:
        logger.warning("Domain error on %s: %s", request.url.path, exc)
        return _err(exc.code, exc.message, 400)

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        logger.exception(
            "Unhandled ValueError on %s — likely a programmer bug or data-integrity problem. "
            "If this is a genuine client input error, raise DomainValidationError instead.",
            request.url.path,
        )
        return _err("INTERNAL_ERROR", "An unexpected internal error occurred.", 500)

    @app.exception_handler(RuntimeError)
    async def runtime_error_handler(request: Request, exc: RuntimeError) -> JSONResponse:
        logger.exception("Internal error on %s", request.url.path)
        return _err("INTERNAL_ERROR", "An unexpected error occurred.", 500)


def _add_utility_routes(app: FastAPI) -> None:
    """Add /health and /ready operational probes."""

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    async def ready() -> JSONResponse:
        """Database readiness probe.

        Intentionally uses a flat ops shape rather than the API error envelope.
        Infrastructure health checks parse this endpoint directly.
        """
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            return JSONResponse(status_code=200, content={"status": "ready"})
        except Exception as exc:
            logger.warning("Readiness check failed: %s", exc)
            return JSONResponse(
                status_code=503, content={"status": "not ready", "database": "unreachable"}
            )


app = create_app()
