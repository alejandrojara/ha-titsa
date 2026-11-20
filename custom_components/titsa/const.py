"""Constants for the TITSA integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "titsa"
INTEGRATION_NAME = "TITSA"
VERSION = "20261120.0"

SUBENTRY_TYPE_STOP = "stop"

# Config / subentry keys
CONF_STOP_ID = "stop_id"
CONF_STOP_NAME = "stop_name"
CONF_STOP_QUERY = "stop_query"
CONF_LINES = "lines"
CONF_SCHEDULE_ENTITY = "schedule_entity"
CONF_SEARCH_AGAIN = "search_again"

# Polling & protection constants
DEFAULT_SCAN_INTERVAL_SECONDS = 90
POLL_JITTER_SECONDS = 10
MAX_BACKOFF_SECONDS = 900
MANUAL_REFRESH_COOLDOWN_SECONDS = 30.0
MAX_CONCURRENT_REQUESTS = 2
HTTP_TIMEOUT_SECONDS = 12.0

# Attribute bounds (Recorder-friendly)
MAX_ARRIVALS_ATTRIBUTE = 12
MAX_LINE_FOLLOWING_ATTRIBUTE = 5

# Open Data catalog storage
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.catalog"
CATALOG_REFRESH_INTERVAL = timedelta(days=30)

# Allowed domains for the optional polling-window schedule entity
SCHEDULE_ENTITY_DOMAINS = ["schedule", "binary_sensor", "input_boolean"]

USER_AGENT = f"HomeAssistant-TITSA/{VERSION} (+https://github.com/alejandrojara/ha-titsa)"
STOP_WEB_URL_TEMPLATE = "https://titsa.com/correspondencias.php?idc={stop_id}"
