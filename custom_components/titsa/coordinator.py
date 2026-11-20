"""DataUpdateCoordinator per TITSA stop subentry with schedule window, jitter, and backoff."""

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
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
    CONF_SCHEDULE_ENTITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MANUAL_REFRESH_COOLDOWN_SECONDS,
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
        raw_schedule = subentry.data.get(CONF_SCHEDULE_ENTITY)
        self.schedule_entity: str | None = str(raw_schedule).strip() if raw_schedule else None

        self._provider = provider
        self._consecutive_errors = 0
        self._last_manual_refresh_monotonic: float | None = None
        self._force_one_shot = False
        self._last_estimation_time: datetime | None = None
        self._unsub_schedule: CALLBACK_TYPE | None = None

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{self.stop_id}",
            update_interval=self._next_poll_interval(),
            always_update=False,
        )

    @staticmethod
    def _next_poll_interval() -> timedelta:
        """Return safe polling interval (90 s) with ±10 s random jitter."""
        jitter = random.uniform(-POLL_JITTER_SECONDS, POLL_JITTER_SECONDS)
        return timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS + jitter)

    @property
    def is_polling_active(self) -> bool:
        """Return True if polling is allowed right now based on optional schedule entity."""
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

    def async_setup_schedule_listener(self) -> None:
        """Start listening to the optional schedule/binary_sensor entity."""
        if not self.schedule_entity or self._unsub_schedule is not None:
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
                self.hass.async_create_task(self.async_request_refresh())
            elif not new_active and old_active:
                _LOGGER.debug(
                    "Schedule entity %s turned OFF for stop %s; pausing polling",
                    self.schedule_entity,
                    self.stop_id,
                )
                self._consecutive_errors = 0
                self.async_set_updated_data(self._build_paused_data())

        self._unsub_schedule = async_track_state_change_event(
            self.hass,
            [self.schedule_entity],
            _handle_schedule_change,
        )

    def async_cleanup(self) -> None:
        """Unsubscribe listeners when unloading the coordinator."""
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

        # Re-jitter next interval to keep requests smoothly staggered
        self.update_interval = self._next_poll_interval()

        polling_active = self.is_polling_active
        if not polling_active and not one_shot:
            _LOGGER.debug("Skipping HTTP poll for stop %s (outside schedule window)", self.stop_id)
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
        return TitsaStopData(
            stop_id=self.stop_id,
            stop_name=self.stop_name,
            estimation_time=snapshot.estimation_time,
            arrivals=snapshot.arrivals,
            filtered_arrivals=filtered,
            polling_paused=not polling_active,
            manual_snapshot=one_shot and not polling_active,
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
