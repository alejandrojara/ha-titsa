"""Unit tests for TitsaWebProvider HTTP behavior, headers, and error mapping."""

from __future__ import annotations

import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.titsa.api import (
    TitsaConnectionError,
    TitsaRateLimitError,
    TitsaStopNotFoundError,
    TitsaWebProvider,
)


def _mock_response(
    status: int,
    body: bytes = b"",
    *,
    headers: dict[str, str] | None = None,
    charset: str | None = "utf-8",
) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    resp.charset = charset
    resp.read = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


async def test_web_provider_success(html_1580: str) -> None:
    """Fetch and parse stop 1580 with honest User-Agent header."""
    session = MagicMock(spec=aiohttp.ClientSession)
    session.get = MagicMock(return_value=_mock_response(200, html_1580.encode("utf-8")))

    provider = TitsaWebProvider(session, semaphore=asyncio.Semaphore(1))
    result = await provider.async_get_stop_arrivals("1580")

    assert result.stop_id == "1580"
    assert len(result.arrivals) == 4
    call_kwargs = session.get.call_args.kwargs
    assert call_kwargs["params"] == {"idc": "1580"}
    assert "HomeAssistant-TITSA" in call_kwargs["headers"]["User-Agent"]


async def test_web_provider_429_rate_limit() -> None:
    """Map HTTP 429 with Retry-After header to TitsaRateLimitError."""
    session = MagicMock(spec=aiohttp.ClientSession)
    session.get = MagicMock(return_value=_mock_response(429, headers={"Retry-After": "180"}))

    provider = TitsaWebProvider(session)
    with pytest.raises(TitsaRateLimitError) as exc_info:
        await provider.async_get_stop_arrivals("1580")

    assert exc_info.value.status == 429
    assert exc_info.value.retry_after == 180.0


async def test_web_provider_503_and_404_and_network_error() -> None:
    """Map HTTP 503, 404, and ClientError cleanly."""
    session = MagicMock(spec=aiohttp.ClientSession)
    provider = TitsaWebProvider(session)

    session.get = MagicMock(return_value=_mock_response(404))
    with pytest.raises(TitsaStopNotFoundError):
        await provider.async_get_stop_arrivals("99999")

    session.get = MagicMock(return_value=_mock_response(503, headers={"Retry-After": "60"}))
    with pytest.raises(TitsaConnectionError) as exc_503:
        await provider.async_get_stop_arrivals("1580")
    assert exc_503.value.status == 503
    assert exc_503.value.retry_after == 60.0

    session.get = MagicMock(side_effect=aiohttp.ClientError("connection reset"))
    with pytest.raises(TitsaConnectionError):
        await provider.async_get_stop_arrivals("1580")


async def test_web_provider_ssl_fallback_on_incomplete_cert_chain(html_1580: str) -> None:
    """Fall back lazily to unverified HTTPS session on ClientConnectorCertificateError and reuse it."""
    verified_session = MagicMock(spec=aiohttp.ClientSession)
    conn_key = MagicMock()
    cert_err = ssl.SSLCertVerificationError(1, "unable to get local issuer certificate")
    ssl_error = aiohttp.ClientConnectorCertificateError(conn_key, cert_err)
    verified_session.get = MagicMock(side_effect=ssl_error)

    unverified_session = MagicMock(spec=aiohttp.ClientSession)
    unverified_session.get = MagicMock(return_value=_mock_response(200, html_1580.encode("utf-8")))
    factory_calls = 0

    def _factory() -> aiohttp.ClientSession:
        nonlocal factory_calls
        factory_calls += 1
        return unverified_session

    provider = TitsaWebProvider(verified_session, fallback_session_factory=_factory)
    assert provider.ssl_fallback_active is False

    # First call fails TLS on verified session, activates fallback, and succeeds immediately
    result = await provider.async_get_stop_arrivals("1580")
    assert result.stop_id == "1580"
    assert len(result.arrivals) == 4
    assert provider.ssl_fallback_active is True
    assert factory_calls == 1
    assert verified_session.get.call_count == 1
    assert unverified_session.get.call_count == 1

    # Second call uses unverified session directly without hitting verified_session again
    result2 = await provider.async_get_stop_arrivals("1580")
    assert result2.stop_id == "1580"
    assert verified_session.get.call_count == 1
    assert unverified_session.get.call_count == 2
    assert factory_calls == 1
