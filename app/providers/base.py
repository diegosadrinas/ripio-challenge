from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class ProviderAttemptResult:
    success: bool
    retryable_error: bool
    provider_status: str
    provider_http_status: int | None
    external_reference: str | None
    response_payload: dict[str, Any] | None
    error_code: str | None = None
    error_message: str | None = None


class ProviderAdapter(ABC):
    name: str
    country_code: str

    def __init__(self, *, base_url: str, timeout_ms: int, max_retries: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries

    @abstractmethod
    def build_payload(self, *, entity_id: str, amount: str, currency: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def normalize_response(self, *, http_status: int, payload: dict[str, Any]) -> ProviderAttemptResult:
        raise NotImplementedError

    def issue_once(self, *, payload: dict[str, Any], correlation_id: str) -> ProviderAttemptResult:
        timeout_seconds = self.timeout_ms / 1000
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(
                f"{self.base_url}/invoices",
                json=payload,
                headers={"X-Correlation-ID": correlation_id},
            )

        data: dict[str, Any]
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text}

        return self.normalize_response(http_status=response.status_code, payload=data)
