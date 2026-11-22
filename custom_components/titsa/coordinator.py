"""DataUpdateCoordinator per TITSA stop subentry with schedule window, manual-only mode, jitter, and backoff."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
import random
import time

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import STATE_ON
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    BusArrival,
    TitsaConnectionError,
    TitsaError,
    TitsaParseError,
    TitsaRateLimitError,
    TitsaRealtimeProvider,
    TitsaStopNotFoundError,
    normalize_line_code,
)
from .const import (
    CONF_LINES,
    CONF_MANUAL_ONLY,
    CONF_SCHEDULE_ENTITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MANUAL_REFRESH_COOLDOWN_SECONDS,
    MANUAL_SNAPSHOT_TTL_SECONDS,
    MAX_BACKOFF_SECONDS,
    POLL_JITTER_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TitsaStopData:
    """Coordinator snapshot for a single TITSA bus stop."""

    stop_id: str
    stop_name: str
    estimation_time: datetime | None
    arrivals: tuple[BusArrival, ...]
    filtered_arrivals: tuple[BusArrival, ...]
    polling_paused: bool
    manual_snapshot: bool = False


class TitsaStopCoordinator(DataUpdateCoordinator[TitsaStopData]):
    """Coordinator managing real-time polling for a single TITSA stop subentry."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        subentry: ConfigSubentry,
        provider: TitsaRealtimeProvider,
    ) -> None:
        """Initialize the stop coordinator."""
        self.subentry = subentry
        self.stop_id: str = str(subentry.data[CONF_STOP_ID]).strip()
        self.stop_name: str = str(
            subentry.data.get(CONF_STOP_NAME) or subentry.title or f"Stop {self.stop_id}"
        )
        raw_lines = subentry.data.get(CONF_LINES) or []
        self.selected_lines: tuple[str, ...] = tuple(
            dict.fromkeys(norm for line in raw_lines if (norm := normalize_line_code(str(line))))
        )
        self._selected_lines_set: frozenset[str] = frozenset(self.selected_lines)
        self.manual_only: bool = bool(subentry.data.get(CONF_MANUAL_ONLY, False))
        raw_schedule = subentry.data.get(CONF_SCHEDULE_ENTITY)
        self.schedule_entity: str | None = (
            None if self.manual_only else (str(raw_schedule).strip() if raw_schedule else None)
        )

        self._provider = provider
        self._consecutive_errors = 0
        self._last_manual_refresh_monotonic: float | None = None
        self._force_one_shot = False
        self._last_estimation_time: datetime | None = None
        self._unsub_schedule: CALLBACK_TYPE | None = None
        self._unsub_manual_expiry: CALLBACK_TYPE | None = None
        self._manual_snapshot_expires_at: datetime | None = None

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{self.stop_id}",
            update_interval=None if self.manual_only else self._next_poll_interval(),
            always_update=False,
        )

    @staticmethod
    def _next_poll_interval() -> timedelta:
        """Return safe polling interval (120 s) with ±10 s random jitter."""
        jitter = random.uniform(-POLL_JITTER_SECONDS, POLL_JITTER_SECONDS)
        return timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS + jitter)

    @property
    def is_polling_active(self) -> bool:
        """Return True if automatic polling is allowed right now."""
        if self.manual_only:
            return False
        if not self.schedule_entity:
            return True
        state = self.hass.states.get(self.schedule_entity)
        return state is not None and state.state == STATE_ON

    def _build_paused_data(self) -> TitsaStopData:
        """Return a clean paused snapshot (unknown state, empty arrivals, no HTTP calls)."""
        return TitsaStopData(
            stop_id=self.stop_id,
            stop_name=self.stop_name,
            estimation_time=self._last_estimation_time,
            arrivals=(),
            filtered_arrivals=(),
            polling_paused=True,
            manual_snapshot=False,
        )

    def _cancel_manual_expiry(self) -> None:
        """Cancel any pending manual snapshot expiry timer."""
        if self._unsub_manual_expiry is not None:
            self._unsub_manual_expiry()
            self._unsub_manual_expiry = None
        self._manual_snapshot_expires_at = None

    def _schedule_manual_expiry(self) -> None:
        """Automatically expire a paused manual snapshot after MANUAL_SNAPSHOT_TTL_SECONDS."""
        self._cancel_manual_expiry()
        self._manual_snapshot_expires_at = dt_util.utcnow() + timedelta(
            seconds=MANUAL_SNAPSHOT_TTL_SECONDS
        )

        @callback
        def _handle_manual_expiry(_now: datetime) -> None:
            self._unsub_manual_expiry = None
            self._manual_snapshot_expires_at = None
            if not self.is_polling_active and self.data is not None and self.data.manual_snapshot:
                _LOGGER.debug(
                    "Manual snapshot expired for stop %s after %ss; returning to paused state",
                    self.stop_id,
                    MANUAL_SNAPSHOT_TTL_SECONDS,
                )
                self.async_set_updated_data(self._build_paused_data())

        self._unsub_manual_expiry = async_call_later(
            self.hass,
            MANUAL_SNAPSHOT_TTL_SECONDS,
            _handle_manual_expiry,
        )

    def async_setup_schedule_listener(self) -> None:
        """Start listening to the optional schedule/binary_sensor entity."""
        if self.manual_only or not self.schedule_entity or self._unsub_schedule is not None:
            return

        @callback
        def _handle_schedule_change(event: Event[EventStateChangedData]) -> None:
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            new_active = new_state is not None and new_state.state == STATE_ON
            old_active = old_state is not None and old_state.state == STATE_ON

            if new_active and not old_active:
                _LOGGER.debug(
                    "Schedule entity %s turned ON for stop %s; requesting immediate refresh",
                    self.schedule_entity,
                    self.stop_id,
                )
                self._cancel_manual_expiry()
                self.hass.async_create_task(self.async_request_refresh())
            elif not new_active and old_active:
                _LOGGER.debug(
                    "Schedule entity %s turned OFF for stop %s; pausing polling",
                    self.schedule_entity,
                    self.stop_id,
                )
                self._cancel_manual_expiry()
                self._consecutive_errors = 0
                self.async_set_updated_data(self._build_paused_data())

        self._unsub_schedule = async_track_state_change_event(
            self.hass,
            [self.schedule_entity],
            _handle_schedule_change,
        )

    def async_cleanup(self) -> None:
        """Unsubscribe listeners and timers when unloading the coordinator."""
        self._cancel_manual_expiry()
        if self._unsub_schedule is not None:
            self._unsub_schedule()
            self._unsub_schedule = None

    def _compute_backoff(self, explicit_retry_after: float | None = None) -> float:
        """Compute capped exponential backoff in seconds."""
        if explicit_retry_after is not None and explicit_retry_after > 0:
            return min(explicit_retry_after, float(MAX_BACKOFF_SECONDS))
        exponent = min(self._consecutive_errors, 4)
        return float(min(DEFAULT_SCAN_INTERVAL_SECONDS * (2**exponent), MAX_BACKOFF_SECONDS))

    async def _async_update_data(self) -> TitsaStopData:
        """Fetch real-time arrivals if within schedule window or triggered manually."""
        one_shot = self._force_one_shot
        self._force_one_shot = False

        # Re-jitter next interval for automatic polling; never enable timer in manual-only mode
        if not self.manual_only:
            self.update_interval = self._next_poll_interval()

        polling_active = self.is_polling_active
        if not polling_active and not one_shot:
            # Preserve an active manual snapshot until its TTL timer expires
            if (
                self.data is not None
                and self.data.manual_snapshot
                and self._manual_snapshot_expires_at is not None
                and dt_util.utcnow() < self._manual_snapshot_expires_at
            ):
                return self.data

            self._cancel_manual_expiry()
            _LOGGER.debug(
                "Skipping HTTP poll for stop %s (%s)",
                self.stop_id,
                "manual-only mode" if self.manual_only else "outside schedule window",
            )
            return self._build_paused_data()

        try:
            snapshot = await self._provider.async_get_stop_arrivals(self.stop_id)
        except TitsaRateLimitError as err:
            self._consecutive_errors += 1
            retry_after = self._compute_backoff(err.retry_after)
            raise UpdateFailed(
                f"Rate limited fetching TITSA stop {self.stop_id}: {err}",
                retry_after=retry_after,
            ) from err
        except (TitsaConnectionError, TitsaParseError, TitsaStopNotFoundError, TitsaError) as err:
            self._consecutive_errors += 1
            explicit = err.retry_after if isinstance(err, TitsaConnectionError) else None
            retry_after = self._compute_backoff(explicit)
            raise UpdateFailed(
                f"Error fetching TITSA stop {self.stop_id}: {err}",
                retry_after=retry_after,
            ) from err

        self._consecutive_errors = 0
        self._last_estimation_time = snapshot.estimation_time
        if snapshot.stop_name and self.stop_name == f"Stop {self.stop_id}":
            self.stop_name = snapshot.stop_name

        filtered = snapshot.filter_lines(self._selected_lines_set)
        is_manual_snapshot = one_shot and not polling_active
        if is_manual_snapshot:
            self._schedule_manual_expiry()
        else:
            self._cancel_manual_expiry()

        return TitsaStopData(
            stop_id=self.stop_id,
            stop_name=self.stop_name,
            estimation_time=snapshot.estimation_time,
            arrivals=snapshot.arrivals,
            filtered_arrivals=filtered,
            polling_paused=not polling_active,
            manual_snapshot=is_manual_snapshot,
        )

    async def async_manual_refresh(self) -> None:
        """Perform an on-demand one-shot refresh with a minimum cooldown."""
        now_mono = time.monotonic()
        if self._last_manual_refresh_monotonic is not None:
            elapsed = now_mono - self._last_manual_refresh_monotonic
            if elapsed < MANUAL_REFRESH_COOLDOWN_SECONDS:
                remaining = math.ceil(MANUAL_REFRESH_COOLDOWN_SECONDS - elapsed)
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="refresh_cooldown",
                    translation_placeholders={"seconds": str(remaining)},
                )

        self._last_manual_refresh_monotonic = now_mono
        self._force_one_shot = True
        try:
            await self.async_refresh()
        finally:
            self._force_one_shot = False

    def get_diagnostics(self) -> dict[str, object]:
        """Return safe diagnostics data for this stop coordinator."""
        schedule_state = (
            state.state
            if self.schedule_entity and (state := self.hass.states.get(self.schedule_entity))
            else None
        )
        return {
            "subentry_id": self.subentry.subentry_id,
            "stop_id": self.stop_id,
            "stop_name": self.stop_name,
            "selected_lines": list(self.selected_lines),
            "manual_only": self.manual_only,
            "schedule_entity": self.schedule_entity,
            "schedule_state": schedule_state,
            "is_polling_active": self.is_polling_active,
            "last_update_success": self.last_update_success,
            "consecutive_errors": self._consecutive_errors,
            "arrivals_count": len(self.data.arrivals) if self.data else 0,
            "filtered_arrivals_count": len(self.data.filtered_arrivals) if self.data else 0,
            "polling_paused": self.data.polling_paused if self.data else False,
            "last_estimation": (
                self.data.estimation_time.isoformat()
                if self.data and self.data.estimation_time
                else None
            ),
        }
