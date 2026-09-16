from fastapi import Header, Request

from app.errors import unauthorized_error


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    settings = request.app.state.settings
    if x_api_key != settings.api_key:
        raise unauthorized_error()
