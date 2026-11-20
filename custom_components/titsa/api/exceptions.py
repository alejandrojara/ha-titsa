"""Exceptions for the decoupled TITSA API client."""

from __future__ import annotations


class TitsaError(Exception):
    """Base exception for TITSA API errors."""


class TitsaConnectionError(TitsaError):
    """Raised when a network or HTTP error occurs talking to TITSA or Open Data."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Initialize connection error."""
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class TitsaRateLimitError(TitsaConnectionError):
    """Raised when the remote server responds with HTTP 429 Too Many Requests."""


class TitsaParseError(TitsaError):
    """Raised when a response cannot be parsed into expected TITSA data."""


class TitsaStopNotFoundError(TitsaError):
    """Raised when a stop ID does not exist or has no valid stop page."""

    def __init__(self, stop_id: str, message: str | None = None) -> None:
        """Initialize stop-not-found error."""
        super().__init__(message or f"TITSA stop '{stop_id}' not found")
        self.stop_id = stop_id
