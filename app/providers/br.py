from typing import Any

from app.providers.base import ProviderAdapter, ProviderAttemptResult


class ProviderBRAdapter(ProviderAdapter):
    name = "provider-br"
    country_code = "BR"

    def build_payload(self, *, entity_id: str, amount: str, currency: str) -> dict[str, Any]:
        return {
            "entity": {"id": entity_id},
            "value": amount,
            "currency_code": currency,
        }

    def normalize_response(self, *, http_status: int, payload: dict[str, Any]) -> ProviderAttemptResult:
        if 200 <= http_status < 300 and payload.get("result") == "issued":
            return ProviderAttemptResult(
                success=True,
                retryable_error=False,
                provider_status="issued",
                provider_http_status=http_status,
                external_reference=payload.get("external_id"),
                response_payload=payload,
            )

        retryable = http_status >= 500 or http_status in (408, 429)
        return ProviderAttemptResult(
            success=False,
            retryable_error=retryable,
            provider_status="error",
            provider_http_status=http_status,
            external_reference=None,
            response_payload=payload,
            error_code="PROVIDER_BR_ERROR",
            error_message=payload.get("error", "provider BR rejected request"),
        )
