import time

from fastapi import FastAPI, Response

app = FastAPI(title="Mock Provider AR")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/invoices")
def issue_invoice(payload: dict, response: Response):
    client_id = payload.get("client_id", "")

    if client_id.startswith("ar-timeout"):
        time.sleep(2)

    if client_id.startswith("ar-fail-permanent"):
        response.status_code = 422
        return {
            "status": "rejected",
            "message": "invalid fiscal data",
        }

    if client_id.startswith("ar-fail-transient"):
        response.status_code = 503
        return {
            "status": "error",
            "message": "temporary provider issue",
        }

    return {
        "status": "ok",
        "reference": f"AR-{client_id}",
    }
