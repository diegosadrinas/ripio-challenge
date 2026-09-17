import hashlib
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from redis import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import ServiceError
from app.models import IdempotencyRecord, IdempotencyState, Invoice, InvoiceAttempt, InvoiceStatus
from app.providers.base import ProviderAdapter, ProviderAttemptResult


logger = logging.getLogger("app.invoice")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return _sha256(canonical)


def _backoff_seconds(attempt: int, base_ms: int, cap_ms: int) -> float:
    raw = min(cap_ms, base_ms * (2 ** (attempt - 1)))
    jitter = random.randint(0, 100)
    return (raw + jitter) / 1000


def _status_to_http(status: InvoiceStatus) -> int:
    if status == InvoiceStatus.ISSUED:
        return 201
    if status == InvoiceStatus.PENDING:
        return 202
    return 422


def _invoice_response(invoice: Invoice) -> dict[str, Any]:
    return {
        "invoice_id": invoice.id,
        "status": invoice.status,
        "provider_used": invoice.provider_used,
        "external_reference": invoice.external_reference,
        "status_reason": invoice.status_reason,
    }


def _short_hash(value: str) -> str:
    return value[:12]


def _is_idempotency_unique_collision(error: IntegrityError) -> bool:
    message = str(error).lower()
    return "unique" in message and "idempotency" in message and "key_hash" in message


class InvoiceService:
    def __init__(self, *, settings: Settings, redis_client: Redis) -> None:
        self.settings = settings
        self.redis = redis_client

    def issue_invoice(
        self,
        *,
        db: Session,
        provider: ProviderAdapter,
        idempotency_key: str,
        request_payload: dict[str, Any],
        correlation_id: str,
    ) -> tuple[dict[str, Any], int]:
        key_hash = _sha256(idempotency_key)
        fingerprint = _request_fingerprint(request_payload)
        lock_key = f"idem-lock:{key_hash}"
        lock_value = str(time.time_ns())

        lock_acquired = bool(
            self.redis.set(
                lock_key,
                lock_value,
                nx=True,
                ex=self.settings.idempotency_lock_ttl_seconds,
            )
        )

        logger.info(
            "idempotency_lock_attempt",
            extra={
                "event": "idempotency_lock_attempt",
                "correlation_id": correlation_id,
                "idempotency_key_hash": _short_hash(key_hash),
                "status": "acquired" if lock_acquired else "busy",
            },
        )

        if not lock_acquired:
            logger.info(
                "idempotency_lock_busy",
                extra={
                    "event": "idempotency_lock_busy",
                    "correlation_id": correlation_id,
                    "idempotency_key_hash": _short_hash(key_hash),
                },
            )
            return self._serve_existing_record_or_in_progress(
                db=db,
                key_hash=key_hash,
                request_fingerprint=fingerprint,
            )

        try:
            return self._issue_invoice_under_lock(
                db=db,
                provider=provider,
                key_hash=key_hash,
                request_fingerprint=fingerprint,
                request_payload=request_payload,
                correlation_id=correlation_id,
            )
        finally:
            current_value = self.redis.get(lock_key)
            if current_value == lock_value:
                self.redis.delete(lock_key)
                logger.info(
                    "idempotency_lock_released",
                    extra={
                        "event": "idempotency_lock_released",
                        "correlation_id": correlation_id,
                        "idempotency_key_hash": _short_hash(key_hash),
                    },
                )

    def get_invoice(self, *, db: Session, invoice_id: str) -> Invoice:
        invoice = db.get(Invoice, invoice_id)
        if not invoice:
            raise ServiceError(
                code="INVOICE_NOT_FOUND",
                message=f"invoice {invoice_id} was not found",
                status_code=404,
                retryable=False,
            )
        return invoice

    def reconcile_stale_pending_invoices(self, *, db: Session, batch_size: int = 200) -> int:
        cutoff = _now_utc() - timedelta(minutes=self.settings.pending_reconciliation_minutes)

        pending_invoices = db.scalars(
            select(Invoice).where(Invoice.status == InvoiceStatus.PENDING).order_by(Invoice.created_at).limit(batch_size)
        ).all()

        updated = 0
        for invoice in pending_invoices:
            created_at = invoice.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            if created_at > cutoff:
                continue

            invoice.status = InvoiceStatus.FAILED
            invoice.status_reason = "RECONCILIATION_TIMEOUT"
            db.add(invoice)
            logger.warning(
                "invoice_reconciliation_timeout",
                extra={
                    "event": "invoice_reconciliation_timeout",
                    "invoice_id": invoice.id,
                    "status": invoice.status,
                },
            )
            updated += 1

        if updated:
            db.commit()

        return updated

    def _serve_existing_record_or_in_progress(
        self,
        *,
        db: Session,
        key_hash: str,
        request_fingerprint: str,
    ) -> tuple[dict[str, Any], int]:
        record = db.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key_hash == key_hash)
        )
        if not record:
            logger.info(
                "idempotency_record_in_progress_without_snapshot",
                extra={
                    "event": "idempotency_record_in_progress_without_snapshot",
                    "idempotency_key_hash": _short_hash(key_hash),
                },
            )
            raise ServiceError(
                code="IDEMPOTENCY_IN_PROGRESS",
                message="another request with the same idempotency key is in progress",
                status_code=202,
                retryable=True,
            )

        if record.request_fingerprint != request_fingerprint:
            logger.warning(
                "idempotency_conflict",
                extra={
                    "event": "idempotency_conflict",
                    "idempotency_key_hash": _short_hash(key_hash),
                    "invoice_id": record.invoice_id,
                },
            )
            raise ServiceError(
                code="IDEMPOTENCY_KEY_CONFLICT",
                message="same idempotency key cannot be reused with different payload",
                status_code=409,
                retryable=False,
            )

        invoice = db.get(Invoice, record.invoice_id)
        if not invoice:
            raise ServiceError(
                code="INVOICE_NOT_FOUND",
                message="invoice not found for idempotency record",
                status_code=404,
                retryable=False,
            )

        if record.state == IdempotencyState.IN_PROGRESS:
            logger.info(
                "idempotency_replay_in_progress",
                extra={
                    "event": "idempotency_replay_in_progress",
                    "idempotency_key_hash": _short_hash(key_hash),
                    "invoice_id": invoice.id,
                },
            )
            payload = _invoice_response(invoice)
            payload["status"] = InvoiceStatus.PENDING
            return payload, 202

        if record.response_snapshot:
            logger.info(
                "idempotency_replay_completed",
                extra={
                    "event": "idempotency_replay_completed",
                    "idempotency_key_hash": _short_hash(key_hash),
                    "invoice_id": invoice.id,
                },
            )
            status = record.response_snapshot.get("status", invoice.status)
            invoice.status = status
            return record.response_snapshot, _status_to_http(invoice.status)

        return _invoice_response(invoice), _status_to_http(invoice.status)

    def _issue_invoice_under_lock(
        self,
        *,
        db: Session,
        provider: ProviderAdapter,
        key_hash: str,
        request_fingerprint: str,
        request_payload: dict[str, Any],
        correlation_id: str,
    ) -> tuple[dict[str, Any], int]:
        existing = db.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key_hash == key_hash)
        )

        if existing:
            if existing.request_fingerprint != request_fingerprint:
                raise ServiceError(
                    code="IDEMPOTENCY_KEY_CONFLICT",
                    message="same idempotency key cannot be reused with different payload",
                    status_code=409,
                    retryable=False,
                )
            invoice = db.get(Invoice, existing.invoice_id)
            if not invoice:
                raise ServiceError(
                    code="INVOICE_NOT_FOUND",
                    message="invoice not found for idempotency record",
                    status_code=404,
                    retryable=False,
                )
            if existing.state == IdempotencyState.IN_PROGRESS:
                payload = _invoice_response(invoice)
                payload["status"] = InvoiceStatus.PENDING
                return payload, 202
            if existing.response_snapshot:
                return existing.response_snapshot, _status_to_http(invoice.status)
            return _invoice_response(invoice), _status_to_http(invoice.status)

        invoice = Invoice(
            entity_id=request_payload["entity_id"],
            amount=request_payload["amount"],
            currency=request_payload["currency"],
            country_code=request_payload["country_code"],
            provider_used=provider.name,
            status=InvoiceStatus.PENDING,
            status_reason="IN_PROGRESS",
            external_reference=None,
        )
        db.add(invoice)
        db.flush()

        record = IdempotencyRecord(
            idempotency_key_hash=key_hash,
            request_fingerprint=request_fingerprint,
            invoice_id=invoice.id,
            response_snapshot=None,
            state=IdempotencyState.IN_PROGRESS,
        )
        db.add(record)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            if _is_idempotency_unique_collision(exc):
                logger.warning(
                    "idempotency_db_collision_replay",
                    extra={
                        "event": "idempotency_db_collision_replay",
                        "correlation_id": correlation_id,
                        "idempotency_key_hash": _short_hash(key_hash),
                    },
                )
                return self._serve_existing_record_or_in_progress(
                    db=db,
                    key_hash=key_hash,
                    request_fingerprint=request_fingerprint,
                )
            raise

        db.refresh(invoice)
        db.refresh(record)

        final_status = InvoiceStatus.PENDING
        final_reason = "PROVIDER_UNCERTAIN_RESULT"
        external_reference: str | None = None

        for attempt_number in range(1, provider.max_retries + 1):
            started_at = _now_utc()
            provider_payload = provider.build_payload(
                entity_id=request_payload["entity_id"],
                amount=str(request_payload["amount"]),
                currency=request_payload["currency"],
            )

            attempt_result: ProviderAttemptResult
            logger.info(
                "provider_attempt_started",
                extra={
                    "event": "provider_attempt_started",
                    "correlation_id": correlation_id,
                    "invoice_id": invoice.id,
                    "provider": provider.name,
                    "attempt_number": attempt_number,
                },
            )
            try:
                attempt_result = provider.issue_once(
                    payload=provider_payload,
                    correlation_id=correlation_id,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError) as exc:
                attempt_result = ProviderAttemptResult(
                    success=False,
                    retryable_error=True,
                    provider_status="timeout_or_network_error",
                    provider_http_status=None,
                    external_reference=None,
                    response_payload={"error": str(exc)},
                    error_code="PROVIDER_TIMEOUT_OR_NETWORK_ERROR",
                    error_message=str(exc),
                )
            except httpx.RequestError as exc:
                attempt_result = ProviderAttemptResult(
                    success=False,
                    retryable_error=True,
                    provider_status="request_error",
                    provider_http_status=None,
                    external_reference=None,
                    response_payload={"error": str(exc)},
                    error_code="PROVIDER_REQUEST_ERROR",
                    error_message=str(exc),
                )

            finished_at = _now_utc()
            duration_ms = int((finished_at - started_at).total_seconds() * 1000)

            attempt = InvoiceAttempt(
                invoice_id=invoice.id,
                attempt_number=attempt_number,
                provider_name=provider.name,
                request_payload=provider_payload,
                response_payload=attempt_result.response_payload,
                http_status=attempt_result.provider_http_status,
                duration_ms=duration_ms,
                attempt_status="success" if attempt_result.success else "error",
                error_type=attempt_result.error_code,
                started_at=started_at,
                finished_at=finished_at,
            )
            db.add(attempt)
            db.commit()

            logger.info(
                "provider_attempt_finished",
                extra={
                    "event": "provider_attempt_finished",
                    "correlation_id": correlation_id,
                    "invoice_id": invoice.id,
                    "provider": provider.name,
                    "attempt_number": attempt_number,
                    "status": "success" if attempt_result.success else "error",
                    "status_code": attempt_result.provider_http_status,
                    "duration_ms": duration_ms,
                    "error_code": attempt_result.error_code,
                    "retryable": attempt_result.retryable_error,
                },
            )

            if attempt_result.success:
                final_status = InvoiceStatus.ISSUED
                final_reason = "ISSUED"
                external_reference = attempt_result.external_reference
                break

            if attempt_result.retryable_error and attempt_number < provider.max_retries:
                time.sleep(
                    _backoff_seconds(
                        attempt=attempt_number,
                        base_ms=self.settings.retry_base_ms,
                        cap_ms=self.settings.retry_cap_ms,
                    )
                )
                continue

            if attempt_result.retryable_error:
                final_status = InvoiceStatus.PENDING
                final_reason = attempt_result.error_code or "PROVIDER_TRANSIENT_FAILURE"
                break

            final_status = InvoiceStatus.FAILED
            final_reason = attempt_result.error_code or "PROVIDER_PERMANENT_FAILURE"
            break

        invoice.status = final_status
        invoice.status_reason = final_reason
        invoice.external_reference = external_reference

        response_snapshot = _invoice_response(invoice)
        record.state = IdempotencyState.COMPLETED
        record.response_snapshot = response_snapshot

        db.add(invoice)
        db.add(record)
        db.commit()
        db.refresh(invoice)

        logger.info(
            "invoice_issue_finalized",
            extra={
                "event": "invoice_issue_finalized",
                "correlation_id": correlation_id,
                "invoice_id": invoice.id,
                "provider": provider.name,
                "status": invoice.status,
                "error_code": invoice.status_reason,
            },
        )

        return response_snapshot, _status_to_http(invoice.status)

    def get_invoice_detail(self, *, db: Session, invoice_id: str) -> dict[str, Any]:
        invoice = self.get_invoice(db=db, invoice_id=invoice_id)

        attempts = db.scalars(
            select(InvoiceAttempt).where(InvoiceAttempt.invoice_id == invoice.id).order_by(InvoiceAttempt.attempt_number)
        ).all()

        return {
            "invoice_id": invoice.id,
            "request_snapshot": {
                "entity_id": invoice.entity_id,
                "amount": str(Decimal(invoice.amount).quantize(Decimal("0.01"))),
                "currency": invoice.currency,
                "country_code": invoice.country_code,
            },
            "status": invoice.status,
            "provider_used": invoice.provider_used,
            "external_reference": invoice.external_reference,
            "status_reason": invoice.status_reason,
            "attempts": [
                {
                    "attempt_number": item.attempt_number,
                    "provider": item.provider_name,
                    "request_payload": item.request_payload,
                    "response_payload": item.response_payload,
                    "http_status": item.http_status,
                    "duration_ms": item.duration_ms,
                    "started_at": item.started_at,
                    "finished_at": item.finished_at,
                    "attempt_status": item.attempt_status,
                }
                for item in attempts
            ],
            "created_at": invoice.created_at,
            "updated_at": invoice.updated_at,
        }
