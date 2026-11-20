"""Realtime arrival providers using the Strategy Pattern (Web today, GTFS-RT tomorrow)."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Callable
from datetime import datetime
import logging
import ssl

import aiohttp

from .exceptions import (
    TitsaConnectionError,
    TitsaRateLimitError,
    TitsaStopNotFoundError,
)
from .models import StopArrivals
from .parser import parse_correspondencias_html
from .utils import parse_retry_after

_LOGGER = logging.getLogger(__name__)

DEFAULT_CORRESPONDENCIAS_URL = "https://titsa.com/correspondencias.php"
DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_USER_AGENT = "HomeAssistant-TITSA/20261120.2 (+https://github.com/alejandrojara/ha-titsa)"


class TitsaRealtimeProvider(ABC):
    """Abstract strategy for fetching real-time arrivals at a TITSA bus stop."""

    @abstractmethod
    async def async_get_stop_arrivals(self, stop_id: str) -> StopArrivals:
        """Fetch real-time bus arrivals for a specific stop ID."""


class TitsaWebProvider(TitsaRealtimeProvider):
    """Web provider scraping the lightweight static table at titsa.com/correspondencias.php."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        fallback_session_factory: Callable[[], aiohttp.ClientSession] | None = None,
        semaphore: asyncio.Semaphore | None = None,
        base_url: str = DEFAULT_CORRESPONDENCIAS_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        """Initialize the web provider."""
        self._session = session
        self._fallback_session_factory = fallback_session_factory
        self._unverified_session: aiohttp.ClientSession | None = None
        self._use_unverified_ssl = False
        self._semaphore = semaphore or asyncio.Semaphore(2)
        self._base_url = base_url
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9",
        }

    @property
    def ssl_fallback_active(self) -> bool:
        """Return True if TLS fallback was activated due to an incomplete server cert chain."""
        return self._use_unverified_ssl

    def _get_active_session(self) -> aiohttp.ClientSession:
        if self._use_unverified_ssl and self._unverified_session is not None:
            return self._unverified_session
        return self._session

    async def _async_fetch_raw(
        self,
        session: aiohttp.ClientSession,
        clean_stop_id: str,
    ) -> tuple[bytes, str]:
        """Perform a single HTTP GET for correspondencias.php?idc=<stop_id>."""
        async with session.get(
            self._base_url,
            params={"idc": clean_stop_id},
            headers=self._headers,
            timeout=self._timeout,
        ) as response:
            if response.status == 429:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                raise TitsaRateLimitError(
                    f"Rate limited by titsa.com (HTTP 429) for stop {clean_stop_id}",
                    status=429,
                    retry_after=retry_after,
                )
            if response.status == 404:
                raise TitsaStopNotFoundError(clean_stop_id)
            if response.status >= 500:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                raise TitsaConnectionError(
                    f"Server error HTTP {response.status} from titsa.com for stop {clean_stop_id}",
                    status=response.status,
                    retry_after=retry_after,
                )
            if response.status != 200:
                raise TitsaConnectionError(
                    f"Unexpected HTTP {response.status} from titsa.com for stop {clean_stop_id}",
                    status=response.status,
                )
            raw_bytes = await response.read()
            charset = response.charset or "utf-8"
            return raw_bytes, charset

    async def async_get_stop_arrivals(
        self,
        stop_id: str,
        *,
        now: datetime | None = None,
    ) -> StopArrivals:
        """Fetch and parse real-time arrivals for a TITSA stop."""
        clean_stop_id = str(stop_id).strip()
        async with self._semaphore:
            try:
                raw_bytes, charset = await self._async_fetch_raw(
                    self._get_active_session(), clean_stop_id
                )
            except (aiohttp.ClientSSLError, ssl.SSLError) as ssl_err:
                if self._use_unverified_ssl or self._fallback_session_factory is None:
                    raise TitsaConnectionError(
                        f"TLS error fetching stop {clean_stop_id} from titsa.com: {ssl_err}"
                    ) from ssl_err

                _LOGGER.warning(
                    "TLS certificate verification failed for %s (%s); "
                    "falling back to unverified HTTPS session for public TITSA arrival tables",
                    self._base_url,
                    ssl_err,
                )
                self._unverified_session = self._fallback_session_factory()
                self._use_unverified_ssl = True
                try:
                    raw_bytes, charset = await self._async_fetch_raw(
                        self._unverified_session, clean_stop_id
                    )
                except (TimeoutError, aiohttp.ClientError) as retry_err:
                    raise TitsaConnectionError(
                        f"Connection error fetching stop {clean_stop_id} from titsa.com: {retry_err}"
                    ) from retry_err
            except (TimeoutError, aiohttp.ClientError) as err:
                raise TitsaConnectionError(
                    f"Connection error fetching stop {clean_stop_id} from titsa.com: {err}"
                ) from err

        try:
            html = raw_bytes.decode(charset)
        except (LookupError, UnicodeDecodeError):
            html = raw_bytes.decode("latin-1", errors="replace")

        return parse_correspondencias_html(html, clean_stop_id, now=now)
