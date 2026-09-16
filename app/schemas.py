from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InvoiceCreateRequest(BaseModel):
    entity_id: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=Decimal("0"))
    currency: str = Field(min_length=3, max_length=3)
    country_code: str = Field(min_length=2, max_length=2)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        normalized = value.upper().strip()
        if not normalized.isalpha() or len(normalized) != 3:
            raise ValueError("currency must be a valid ISO-4217 alpha code")
        return normalized

    @field_validator("country_code")
    @classmethod
    def validate_country_code(cls, value: str) -> str:
        normalized = value.upper().strip()
        if not normalized.isalpha() or len(normalized) != 2:
            raise ValueError("country_code must be a valid ISO alpha-2 code")
        return normalized


class InvoiceCreateResponse(BaseModel):
    invoice_id: str
    status: Literal["issued", "pending", "failed"]
    provider_used: str
    external_reference: str | None = None
    status_reason: str | None = None


class InvoiceAttemptResponse(BaseModel):
    attempt_number: int
    provider: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any] | None
    http_status: int | None
    duration_ms: int
    started_at: datetime
    finished_at: datetime
    attempt_status: str


class InvoiceDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: str
    request_snapshot: dict[str, Any]
    status: Literal["issued", "pending", "failed"]
    provider_used: str
    external_reference: str | None
    status_reason: str | None
    attempts: list[InvoiceAttemptResponse]
    created_at: datetime
    updated_at: datetime


class ProviderInfoResponse(BaseModel):
    country_code: str
    provider_name: str
    timeout_ms: int
    retry_policy: dict[str, Any]


class ProvidersListResponse(BaseModel):
    providers: list[ProviderInfoResponse]


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None
    correlation_id: str
    retryable: bool = False
