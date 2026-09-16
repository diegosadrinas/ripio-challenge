import json
import logging
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    def __init__(self, *, app_name: str, app_env: str) -> None:
        super().__init__()
        self.app_name = app_name
        self.app_env = app_env

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "app_name": self.app_name,
            "app_env": self.app_env,
        }

        for key in (
            "event",
            "correlation_id",
            "invoice_id",
            "idempotency_key_hash",
            "provider",
            "attempt_number",
            "duration_ms",
            "status_code",
            "status",
            "path",
            "method",
            "error_code",
            "retryable",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=True, default=str)


def configure_logging(*, level: str, app_name: str, app_env: str) -> None:
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(app_name=app_name, app_env=app_env))
    root_logger.addHandler(handler)

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(numeric_level)

    # Keep third-party loggers at warning level unless explicitly raised.
    logging.getLogger("httpx").setLevel(max(logging.WARNING, numeric_level))
    logging.getLogger("urllib3").setLevel(max(logging.WARNING, numeric_level))
