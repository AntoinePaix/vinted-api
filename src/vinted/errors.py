"""Exceptions raised by the Vinted clients."""

from __future__ import annotations

from typing import Any


class VintedError(Exception):
    """Raised when Vinted answers with an error."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: Any = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.retry_after = retry_after  # seconds, from the Retry-After header when present


class NotFoundError(VintedError):
    """The item, user or brand does not exist (HTTP 404)."""


class RateLimitError(VintedError):
    """Vinted is throttling requests (HTTP 429)."""


class BlockedError(VintedError):
    """Vinted (Cloudflare) refused the request (HTTP 403), usually because of the IP address."""


class NetworkError(VintedError):
    """The request did not get an answer: DNS, connection, proxy or timeout error."""
