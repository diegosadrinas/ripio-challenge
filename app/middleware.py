import logging
import time
import uuid

from fastapi import Request


CORRELATION_HEADER = "X-Correlation-ID"
request_logger = logging.getLogger("app.request")


def _metrics_path(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if route_path:
        return route_path
    return request.url.path


def _record_request_metric(request: Request, *, status_code: int, duration_ms: int) -> None:
    collector = getattr(request.app.state, "metrics", None)
    if collector is None:
        return

    collector.observe_request(
        method=request.method,
        path=_metrics_path(request),
        status_code=status_code,
        duration_ms=duration_ms,
    )


async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get(CORRELATION_HEADER, str(uuid.uuid4()))
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers[CORRELATION_HEADER] = correlation_id
    return response


async def request_logging_middleware(request: Request, call_next):
    start = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = int((time.perf_counter() - start) * 1000)
        _record_request_metric(request, status_code=500, duration_ms=duration_ms)
        request_logger.exception(
            "request_failed",
            extra={
                "event": "request_failed",
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
                "correlation_id": getattr(request.state, "correlation_id", "unknown"),
            },
        )
        raise

    duration_ms = int((time.perf_counter() - start) * 1000)
    _record_request_metric(request, status_code=response.status_code, duration_ms=duration_ms)
    request_logger.info(
        "request_completed",
        extra={
            "event": "request_completed",
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "correlation_id": getattr(request.state, "correlation_id", "unknown"),
        },
    )
    return response
