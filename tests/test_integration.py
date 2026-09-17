from datetime import datetime, timedelta, timezone

import httpx
import respx
from sqlalchemy.exc import IntegrityError

from app.models import Invoice, InvoiceStatus


def _headers(idempotency_key: str) -> dict[str, str]:
    return {
        "X-API-Key": "test-api-key",
        "X-Idempotency-Key": idempotency_key,
    }


def test_issue_invoice_success_and_audit_trail(client):
    payload = {
        "entity_id": "entity-ar-1",
        "amount": "1500.00",
        "currency": "ARS",
        "country_code": "AR",
    }

    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://provider-ar.test/invoices").mock(
            return_value=httpx.Response(
                200,
                json={"status": "ok", "reference": "AR-entity-ar-1"},
            )
        )

        response = client.post("/invoices", json=payload, headers=_headers("idem-ar-1"))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "issued"
    assert body["provider_used"] == "provider-ar"
    assert body["external_reference"] == "AR-entity-ar-1"

    invoice_id = body["invoice_id"]
    detail_response = client.get(f"/invoices/{invoice_id}", headers={"X-API-Key": "test-api-key"})
    assert detail_response.status_code == 200

    detail = detail_response.json()
    assert detail["status"] == "issued"
    assert len(detail["attempts"]) == 1
    assert detail["attempts"][0]["provider"] == "provider-ar"


def test_provider_transient_failure_returns_pending(client):
    payload = {
        "entity_id": "entity-br-transient",
        "amount": "99.99",
        "currency": "BRL",
        "country_code": "BR",
    }

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://provider-br.test/invoices")
        route.side_effect = [
            httpx.Response(503, json={"error": "temporary outage"}),
            httpx.Response(503, json={"error": "temporary outage"}),
            httpx.Response(503, json={"error": "temporary outage"}),
        ]

        response = client.post("/invoices", json=payload, headers=_headers("idem-br-1"))

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["provider_used"] == "provider-br"

    detail = client.get(f"/invoices/{body['invoice_id']}", headers={"X-API-Key": "test-api-key"}).json()
    assert detail["status"] == "pending"
    assert len(detail["attempts"]) == 3


def test_idempotency_hit_returns_same_invoice_without_new_provider_call(client):
    payload = {
        "entity_id": "entity-ar-idem",
        "amount": "10.00",
        "currency": "ARS",
        "country_code": "AR",
    }

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://provider-ar.test/invoices").mock(
            return_value=httpx.Response(
                200,
                json={"status": "ok", "reference": "AR-entity-ar-idem"},
            )
        )

        first = client.post("/invoices", json=payload, headers=_headers("idem-same-key"))
        second = client.post("/invoices", json=payload, headers=_headers("idem-same-key"))

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["invoice_id"] == second.json()["invoice_id"]
    assert route.call_count == 1


def test_idempotency_conflict_when_payload_changes(client):
    first_payload = {
        "entity_id": "entity-ar-conflict",
        "amount": "10.00",
        "currency": "ARS",
        "country_code": "AR",
    }
    second_payload = {
        "entity_id": "entity-ar-conflict",
        "amount": "12.00",
        "currency": "ARS",
        "country_code": "AR",
    }

    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://provider-ar.test/invoices").mock(
            return_value=httpx.Response(
                200,
                json={"status": "ok", "reference": "AR-entity-ar-conflict"},
            )
        )

        first = client.post("/invoices", json=first_payload, headers=_headers("idem-conflict"))
        second = client.post("/invoices", json=second_payload, headers=_headers("idem-conflict"))

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_metrics_endpoint_reports_http_aggregates(client):
    payload = {
        "entity_id": "entity-ar-metrics",
        "amount": "20.00",
        "currency": "ARS",
        "country_code": "AR",
    }

    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://provider-ar.test/invoices").mock(
            return_value=httpx.Response(
                200,
                json={"status": "ok", "reference": "AR-entity-ar-metrics"},
            )
        )

        create_response = client.post("/invoices", json=payload, headers=_headers("idem-metrics-1"))

    assert create_response.status_code == 201

    metrics_response = client.get("/metrics", headers={"X-API-Key": "test-api-key"})
    assert metrics_response.status_code == 200

    body = metrics_response.json()
    assert body["requests_total"] >= 1
    assert body["requests_by_status_family"]["2xx"] >= 1

    invoices_metric = next(
        (
            item
            for item in body["request_metrics"]
            if item["method"] == "POST"
            and item["path"] == "/invoices"
            and item["status_code"] == 201
        ),
        None,
    )
    assert invoices_metric is not None
    assert invoices_metric["count"] == 1


def test_idempotency_db_collision_replays_existing_record(client, monkeypatch):
    service = client.app.state.invoice_service
    provider = client.app.state.provider_registry.resolve("AR")

    request_payload = {
        "entity_id": "entity-ar-collision",
        "amount": "20.00",
        "currency": "ARS",
        "country_code": "AR",
    }

    expected_payload = {
        "invoice_id": "existing-invoice",
        "status": "issued",
        "provider_used": "provider-ar",
        "external_reference": "AR-existing",
        "status_reason": "ISSUED",
    }

    db_session = client.app.state.db.session()
    try:
        commit_calls = {"count": 0}

        original_commit = db_session.commit

        def flaky_commit():
            commit_calls["count"] += 1
            if commit_calls["count"] == 1:
                raise IntegrityError(
                    statement="insert",
                    params={},
                    orig=Exception("UNIQUE constraint failed: idempotency_records.idempotency_key_hash"),
                )
            return original_commit()

        def replay_existing(*, db, key_hash, request_fingerprint):
            assert db is db_session
            assert key_hash == "fake-key-hash"
            assert request_fingerprint == "fake-fingerprint"
            return expected_payload, 201

        monkeypatch.setattr(db_session, "commit", flaky_commit)
        monkeypatch.setattr(service, "_serve_existing_record_or_in_progress", replay_existing)

        result, status_code = service._issue_invoice_under_lock(
            db=db_session,
            provider=provider,
            key_hash="fake-key-hash",
            request_fingerprint="fake-fingerprint",
            request_payload=request_payload,
            correlation_id="test-correlation-id",
        )

        assert status_code == 201
        assert result == expected_payload
        assert commit_calls["count"] == 1
    finally:
        db_session.close()


def test_get_invoice_detail_does_not_reconcile_pending(client):
    db_session = client.app.state.db.session()
    stale_time = datetime.now(timezone.utc) - timedelta(hours=2)

    invoice = Invoice(
        entity_id="entity-stale",
        amount=10,
        currency="ARS",
        country_code="AR",
        provider_used="provider-ar",
        status=InvoiceStatus.PENDING,
        status_reason="PROVIDER_UNCERTAIN_RESULT",
        external_reference=None,
        created_at=stale_time,
        updated_at=stale_time,
    )

    try:
        db_session.add(invoice)
        db_session.commit()
        invoice_id = invoice.id
    finally:
        db_session.close()

    response = client.get(f"/invoices/{invoice_id}", headers={"X-API-Key": "test-api-key"})
    assert response.status_code == 200
    assert response.json()["status"] == "pending"

    verify_session = client.app.state.db.session()
    try:
        persisted = verify_session.get(Invoice, invoice_id)
        assert persisted is not None
        assert persisted.status == InvoiceStatus.PENDING
        assert persisted.status_reason == "PROVIDER_UNCERTAIN_RESULT"
    finally:
        verify_session.close()
