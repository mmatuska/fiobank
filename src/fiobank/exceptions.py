from __future__ import annotations


class ThrottlingError(Exception):
    """Throttling error raised when the API is being used too fast."""

    def __str__(self) -> str:
        return "Token can be used only once per 30s"


class HTTPError(IOError):
    """Raised when an HTTP request returns a non-2xx status code."""

    def __init__(self, message: str, *, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(message)
