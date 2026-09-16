from fastapi import status


class ServiceError(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        details: dict | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        self.retryable = retryable


def unauthorized_error() -> ServiceError:
    return ServiceError(
        code="UNAUTHORIZED",
        message="missing or invalid API key",
        status_code=status.HTTP_401_UNAUTHORIZED,
        retryable=False,
    )
