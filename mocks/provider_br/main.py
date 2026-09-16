import time

from fastapi import FastAPI, Response

app = FastAPI(title="Mock Provider BR")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/invoices")
def issue_invoice(payload: dict, response: Response):
    entity_id = payload.get("entity", {}).get("id", "")

    if entity_id.startswith("br-timeout"):
        time.sleep(2)

    if entity_id.startswith("br-fail-permanent"):
        response.status_code = 422
        return {
            "error": "invalid document",
        }

    if entity_id.startswith("br-fail-transient"):
        response.status_code = 503
        return {
            "error": "service unavailable",
        }

    return {
        "result": "issued",
        "external_id": f"BR-{entity_id}",
    }
