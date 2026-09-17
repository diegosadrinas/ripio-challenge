import asyncio
from contextlib import asynccontextmanager, suppress
import logging
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.config import Settings, get_settings
from app.db import Database, get_db
from app.errors import ServiceError
from app.logging_config import configure_logging
from app.metrics import InMemoryMetricsCollector
from app.middleware import correlation_id_middleware, request_logging_middleware
from app.providers.registry import ProviderRegistry
from app.redis_client import build_redis_client
from app.schemas import (
    AppMetricsResponse,
    ErrorResponse,
    InvoiceCreateRequest,
    InvoiceCreateResponse,
    InvoiceDetailResponse,
    ProviderInfoResponse,
    ProvidersListResponse,
)
from app.services.invoice_service import InvoiceService


def _correlation_id_from_request(request: Request) -> str:
    return getattr(request.state, "correlation_id", "unknown")


async def _pending_reconciliation_loop(app: FastAPI, app_logger: logging.Logger) -> None:
    interval = app.state.settings.pending_reconciliation_poll_seconds
    if interval <= 0:
        return

    invoice_service: InvoiceService = app.state.invoice_service
    while True:
        db_session = app.state.db.session()
        try:
            updated = invoice_service.reconcile_stale_pending_invoices(db=db_session)
            if updated > 0:
                app_logger.info(
                    "pending_reconciliation_batch",
                    extra={
                        "event": "pending_reconciliation_batch",
                        "status": "updated",
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            app_logger.exception(
                "pending_reconciliation_failed",
                extra={"event": "pending_reconciliation_failed"},
            )
        finally:
            db_session.close()

        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None, redis_client: Redis | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    configure_logging(
        level=runtime_settings.log_level,
        app_name=runtime_settings.app_name,
        app_env=runtime_settings.app_env,
    )
    app_logger = logging.getLogger("app.main")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app_logger.info("app_starting", extra={"event": "app_starting"})
        app.state.db.init_models()

        reconciliation_task: asyncio.Task[None] | None = None
        if app.state.settings.pending_reconciliation_poll_seconds > 0:
            reconciliation_task = asyncio.create_task(_pending_reconciliation_loop(app, app_logger))

        yield

        if reconciliation_task:
            reconciliation_task.cancel()
            with suppress(asyncio.CancelledError):
                await reconciliation_task

        app_logger.info("app_stopping", extra={"event": "app_stopping"})

    app = FastAPI(
        title="Multi-Country Invoicing Service",
        version="1.0.0",
        description="REST API for multi-country invoice issuance with provider routing and audit trail.",
        lifespan=lifespan,
    )

    app.middleware("http")(correlation_id_middleware)
    app.middleware("http")(request_logging_middleware)

    db = Database(runtime_settings.database_url)

    app.state.settings = runtime_settings
    app.state.db = db
    app.state.redis = redis_client or build_redis_client(runtime_settings.redis_url)
    app.state.metrics = InMemoryMetricsCollector()
    app.state.provider_registry = ProviderRegistry(runtime_settings)
    app.state.invoice_service = InvoiceService(
        settings=runtime_settings,
        redis_client=app.state.redis,
    )

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        app_logger.warning(
            "service_error",
            extra={
                "event": "service_error",
                "correlation_id": _correlation_id_from_request(request),
                "error_code": exc.code,
                "status_code": exc.status_code,
                "retryable": exc.retryable,
            },
        )
        payload = ErrorResponse(
            code=exc.code,
            message=exc.message,
            details=exc.details,
            retryable=exc.retryable,
            correlation_id=_correlation_id_from_request(request),
        )
        return JSONResponse(status_code=exc.status_code, content=payload.model_dump())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        app_logger.warning(
            "validation_error",
            extra={
                "event": "validation_error",
                "correlation_id": _correlation_id_from_request(request),
                "status_code": 422,
                "error_code": "VALIDATION_ERROR",
            },
        )
        payload = ErrorResponse(
            code="VALIDATION_ERROR",
            message="request validation failed",
            details={"errors": exc.errors()},
            retryable=False,
            correlation_id=_correlation_id_from_request(request),
        )
        return JSONResponse(status_code=422, content=payload.model_dump())

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/metrics",
        response_model=AppMetricsResponse,
        responses={401: {"model": ErrorResponse}},
        dependencies=[Depends(require_api_key)],
    )
    def get_metrics(request: Request) -> AppMetricsResponse:
        metrics_payload = request.app.state.metrics.snapshot()
        return AppMetricsResponse(**metrics_payload)

    @app.post(
        "/invoices",
        response_model=InvoiceCreateResponse,
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
        },
        dependencies=[Depends(require_api_key)],
    )
    def create_invoice(
        invoice_request: InvoiceCreateRequest,
        request: Request,
        db_session: Session = Depends(get_db),
        idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    ) -> JSONResponse:
        if not idempotency_key:
            raise ServiceError(
                code="MISSING_IDEMPOTENCY_KEY",
                message="header X-Idempotency-Key is required",
                status_code=400,
                retryable=False,
            )

        if len(idempotency_key) > 128:
            raise ServiceError(
                code="INVALID_IDEMPOTENCY_KEY",
                message="X-Idempotency-Key must be <= 128 chars",
                status_code=400,
                retryable=False,
            )

        provider_registry: ProviderRegistry = request.app.state.provider_registry
        provider = provider_registry.resolve(invoice_request.country_code)

        invoice_service: InvoiceService = request.app.state.invoice_service
        result, status_code = invoice_service.issue_invoice(
            db=db_session,
            provider=provider,
            idempotency_key=idempotency_key,
            request_payload=invoice_request.model_dump(),
            correlation_id=_correlation_id_from_request(request),
        )

        response = InvoiceCreateResponse(**result)
        return JSONResponse(status_code=status_code, content=response.model_dump())

    @app.get(
        "/invoices/{invoice_id}",
        response_model=InvoiceDetailResponse,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
        dependencies=[Depends(require_api_key)],
    )
    def get_invoice(
        invoice_id: str,
        request: Request,
        db_session: Session = Depends(get_db),
    ) -> InvoiceDetailResponse:
        invoice_service: InvoiceService = request.app.state.invoice_service
        detail = invoice_service.get_invoice_detail(db=db_session, invoice_id=invoice_id)
        return InvoiceDetailResponse(**detail)

    @app.get(
        "/providers",
        response_model=ProvidersListResponse,
        responses={401: {"model": ErrorResponse}},
        dependencies=[Depends(require_api_key)],
    )
    def get_providers(request: Request) -> ProvidersListResponse:
        provider_registry: ProviderRegistry = request.app.state.provider_registry
        providers = [ProviderInfoResponse(**item) for item in provider_registry.list_supported()]
        return ProvidersListResponse(providers=providers)

    return app


app = create_app()
