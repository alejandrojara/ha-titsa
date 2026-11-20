"""Config flow and stop subentry flow for the TITSA integration."""

from __future__ import annotations

import logging
from typing import Any, override

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from . import TitsaConfigEntry
from .api import (
    StopInfo,
    TitsaError,
    TitsaStopNotFoundError,
    line_sort_key,
    normalize_line_code,
)
from .const import (
    CONF_LINES,
    CONF_SCHEDULE_ENTITY,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_STOP_QUERY,
    DOMAIN,
    INTEGRATION_NAME,
    SCHEDULE_ENTITY_DOMAINS,
    SUBENTRY_TYPE_STOP,
)

_LOGGER = logging.getLogger(__name__)


def _flow_schema(schema: vol.Schema) -> Any:
    """Adapt voluptuous.Schema for HA 2026 probatio typing while staying HA 2025.6+ compatible."""
    return schema


class TitsaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle parent config flow for TITSA."""

    VERSION = 1
    MINOR_VERSION = 1

    @override
    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: TitsaConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentry types supported by TITSA."""
        return {SUBENTRY_TYPE_STOP: TitsaStopSubentryFlow}

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Set up the single parent TITSA hub entry."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            return self.async_create_entry(title=INTEGRATION_NAME, data={})

        return self.async_show_form(step_id="user", data_schema=_flow_schema(vol.Schema({})))


class TitsaStopSubentryFlow(ConfigSubentryFlow):
    """Handle adding and reconfiguring TITSA bus stop subentries."""

    def __init__(self) -> None:
        """Initialize subentry flow state."""
        super().__init__()
        self._stop_id: str | None = None
        self._stop_name: str | None = None
        self._matching_stops: list[StopInfo] = []
        self._live_lines: list[str] = []

    def _get_parent_entry(self) -> TitsaConfigEntry:
        return self._get_entry()

    def _is_stop_already_configured(
        self, stop_id: str, *, exclude_subentry_id: str | None = None
    ) -> bool:
        entry = self._get_parent_entry()
        for subentry in entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_TYPE_STOP:
                continue
            if exclude_subentry_id and subentry.subentry_id == exclude_subentry_id:
                continue
            if subentry.unique_id == stop_id or str(subentry.data.get(CONF_STOP_ID)) == stop_id:
                return True
        return False

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Step 1: Search for a stop by numeric code or name."""
        entry = self._get_parent_entry()
        runtime_data = entry.runtime_data
        catalog = runtime_data.catalog
        errors: dict[str, str] = {}

        # Ensure lightweight stops/lines CSV is loaded before searching
        await catalog.async_ensure_csv_fresh()

        if user_input is not None:
            query = str(user_input.get(CONF_STOP_QUERY) or "").strip()
            if not query:
                errors[CONF_STOP_QUERY] = "stop_not_found"
            elif query.isdigit():
                cached_stop = catalog.get_stop(query)
                if cached_stop is not None:
                    self._stop_id = cached_stop.stop_id
                    self._stop_name = cached_stop.name
                    if self._is_stop_already_configured(self._stop_id):
                        return self.async_abort(reason="already_configured")
                    return await self.async_step_configure()

                # Stop not in CSV cache (new stop or Open Data offline): validate live
                try:
                    snapshot = await runtime_data.provider.async_get_stop_arrivals(query)
                except TitsaStopNotFoundError:
                    errors[CONF_STOP_QUERY] = "stop_not_found"
                except TitsaError:
                    errors["base"] = "cannot_connect"
                else:
                    self._stop_id = snapshot.stop_id
                    self._stop_name = snapshot.stop_name
                    self._live_lines = [a.line for a in snapshot.arrivals]
                    if self._is_stop_already_configured(self._stop_id):
                        return self.async_abort(reason="already_configured")
                    return await self.async_step_configure()
            else:
                matches = catalog.search_stops(query, limit=50)
                if not matches:
                    errors[CONF_STOP_QUERY] = "stop_not_found"
                elif len(matches) == 1:
                    stop = matches[0]
                    self._stop_id = stop.stop_id
                    self._stop_name = stop.name
                    if self._is_stop_already_configured(self._stop_id):
                        return self.async_abort(reason="already_configured")
                    return await self.async_step_configure()
                else:
                    self._matching_stops = matches
                    return await self.async_step_select_stop()

        schema = vol.Schema(
            {
                vol.Required(CONF_STOP_QUERY): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                )
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_flow_schema(schema), user_input),
            errors=errors,
        )

    async def async_step_select_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 1b: Disambiguate when a text search matches multiple stops."""
        entry = self._get_parent_entry()
        catalog = entry.runtime_data.catalog

        if user_input is not None:
            selected_id = str(user_input[CONF_STOP_ID]).strip()
            if self._is_stop_already_configured(selected_id):
                return self.async_abort(reason="already_configured")

            stop = catalog.get_stop(selected_id)
            self._stop_id = selected_id
            self._stop_name = stop.name if stop else f"Stop {selected_id}"
            return await self.async_step_configure()

        options: list[SelectOptionDict] = []
        for stop in self._matching_stops:
            routes = catalog.data.stop_routes.get(stop.stop_id)
            if routes:
                preview = ", ".join(routes[:6])
                if len(routes) > 6:
                    preview = f"{preview}…"
                label = f"{stop.stop_id} — {stop.name} ({preview})"
            else:
                label = f"{stop.stop_id} — {stop.name}"
            options.append(SelectOptionDict(value=stop.stop_id, label=label))

        schema = vol.Schema(
            {
                vol.Required(CONF_STOP_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="select_stop", data_schema=_flow_schema(schema))

    async def async_step_configure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 2: Configure name, line filter, and optional schedule window."""
        entry = self._get_parent_entry()
        runtime_data = entry.runtime_data
        catalog = runtime_data.catalog
        reconfigure_subentry = (
            self._get_reconfigure_subentry() if self.source == SOURCE_RECONFIGURE else None
        )

        stop_id = self._stop_id
        stop_name = self._stop_name
        if stop_id is None and reconfigure_subentry is not None:
            stop_id = str(reconfigure_subentry.data[CONF_STOP_ID])
            stop_name = str(
                reconfigure_subentry.data.get(CONF_STOP_NAME)
                or reconfigure_subentry.title
                or f"Stop {stop_id}"
            )
            self._stop_id = stop_id
            self._stop_name = stop_name

        assert stop_id is not None
        stop_name = stop_name or f"Stop {stop_id}"

        if self._is_stop_already_configured(
            stop_id,
            exclude_subentry_id=(
                reconfigure_subentry.subentry_id if reconfigure_subentry else None
            ),
        ):
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            title = str(user_input.get(CONF_NAME) or "").strip() or f"{stop_name} ({stop_id})"
            raw_lines = user_input.get(CONF_LINES) or []
            if isinstance(raw_lines, str):
                raw_lines = [raw_lines]
            clean_lines = sorted(
                {norm for line in raw_lines if (norm := normalize_line_code(str(line)))},
                key=line_sort_key,
            )
            raw_schedule = user_input.get(CONF_SCHEDULE_ENTITY)
            schedule_entity = str(raw_schedule).strip() if raw_schedule else None

            subentry_data: dict[str, Any] = {
                CONF_STOP_ID: stop_id,
                CONF_STOP_NAME: stop_name,
                CONF_LINES: clean_lines,
                CONF_SCHEDULE_ENTITY: schedule_entity,
            }

            if reconfigure_subentry is not None:
                return self.async_update_and_abort(
                    entry,
                    reconfigure_subentry,
                    title=title,
                    unique_id=stop_id,
                    data=subentry_data,
                )

            return self.async_create_entry(
                title=title,
                unique_id=stop_id,
                data=subentry_data,
            )

        # Hybrid route lookup: if compact GTFS index doesn't have this stop yet,
        # opportunistically check live arrivals once to surface active lines
        existing_lines: list[str] = (
            list(reconfigure_subentry.data.get(CONF_LINES) or [])
            if reconfigure_subentry is not None
            else []
        )
        if stop_id not in catalog.data.stop_routes and not self._live_lines:
            try:
                snapshot = await runtime_data.provider.async_get_stop_arrivals(stop_id)
                self._live_lines = [a.line for a in snapshot.arrivals]
            except TitsaError:
                _LOGGER.debug(
                    "Could not fetch live lines for stop %s during config flow; using catalog fallback",
                    stop_id,
                )

        candidate_lines, _ = catalog.get_routes_for_stop(
            stop_id,
            live_lines=self._live_lines,
            extra_lines=existing_lines,
        )
        line_options = [
            SelectOptionDict(value=code, label=catalog.format_line_label(code))
            for code in candidate_lines
        ]

        default_name = (
            reconfigure_subentry.title
            if reconfigure_subentry is not None
            else f"{stop_name} ({stop_id})"
        )
        suggested_values: dict[str, Any] = {
            CONF_NAME: default_name,
            CONF_LINES: existing_lines,
        }
        if reconfigure_subentry is not None and reconfigure_subentry.data.get(CONF_SCHEDULE_ENTITY):
            suggested_values[CONF_SCHEDULE_ENTITY] = reconfigure_subentry.data[CONF_SCHEDULE_ENTITY]

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_LINES, default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=line_options,
                        multiple=True,
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_SCHEDULE_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain=SCHEDULE_ENTITY_DOMAINS)
                ),
            }
        )
        return self.async_show_form(
            step_id="configure",
            data_schema=self.add_suggested_values_to_schema(_flow_schema(schema), suggested_values),
            description_placeholders={
                "stop_id": stop_id,
                "stop_name": stop_name,
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure an existing stop subentry (name, line filter, schedule window)."""
        subentry = self._get_reconfigure_subentry()
        self._stop_id = str(subentry.data[CONF_STOP_ID])
        self._stop_name = str(
            subentry.data.get(CONF_STOP_NAME) or subentry.title or f"Stop {self._stop_id}"
        )
        return await self.async_step_configure(user_input)
