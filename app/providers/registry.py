from app.config import Settings
from app.errors import ServiceError
from app.providers.ar import ProviderARAdapter
from app.providers.base import ProviderAdapter
from app.providers.br import ProviderBRAdapter


class ProviderRegistry:
    def __init__(self, settings: Settings) -> None:
        self._providers: dict[str, ProviderAdapter] = {
            "AR": ProviderARAdapter(
                base_url=settings.provider_ar_base_url,
                timeout_ms=settings.provider_ar_timeout_ms,
                max_retries=settings.provider_ar_max_retries,
            ),
            "BR": ProviderBRAdapter(
                base_url=settings.provider_br_base_url,
                timeout_ms=settings.provider_br_timeout_ms,
                max_retries=settings.provider_br_max_retries,
            ),
        }
        self._retry_policy = {
            "retry_base_ms": settings.retry_base_ms,
            "retry_cap_ms": settings.retry_cap_ms,
        }

    def resolve(self, country_code: str) -> ProviderAdapter:
        provider = self._providers.get(country_code)
        if provider is None:
            raise ServiceError(
                code="UNSUPPORTED_COUNTRY",
                message=f"country {country_code} is not configured",
                status_code=422,
                retryable=False,
            )
        return provider

    def list_supported(self) -> list[dict]:
        items: list[dict] = []
        for country_code, provider in sorted(self._providers.items()):
            items.append(
                {
                    "country_code": country_code,
                    "provider_name": provider.name,
                    "timeout_ms": provider.timeout_ms,
                    "retry_policy": {
                        "max_retries": provider.max_retries,
                        **self._retry_policy,
                    },
                }
            )
        return items
