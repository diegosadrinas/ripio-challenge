import httpx
import respx


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
