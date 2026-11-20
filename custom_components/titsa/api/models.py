"""Data models for the decoupled TITSA API client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class BusArrival:
    """Single real-time bus arrival estimate at a TITSA stop."""

    line: str
    line_name: str
    destination: str
    minutes: int
    estimated_arrival: datetime
    is_approximate: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize arrival for Home Assistant entity attributes."""
        return {
            "line": self.line,
            "line_name": self.line_name,
            "destination": self.destination,
            "minutes": self.minutes,
            "estimated_arrival": self.estimated_arrival.isoformat(),
            "is_approximate": self.is_approximate,
        }


@dataclass(frozen=True, slots=True)
class StopArrivals:
    """Real-time arrivals snapshot for a TITSA bus stop."""

    stop_id: str
    stop_name: str
    estimation_time: datetime
    arrivals: tuple[BusArrival, ...] = ()
    estimation_from_server: bool = True

    def filter_lines(self, allowed_lines: set[str] | frozenset[str]) -> tuple[BusArrival, ...]:
        """Return arrivals matching allowed normalized lines, or all if empty."""
        if not allowed_lines:
            return self.arrivals
        return tuple(a for a in self.arrivals if a.line in allowed_lines)


@dataclass(frozen=True, slots=True)
class StopInfo:
    """Metadata for a TITSA bus stop from Cabildo de Tenerife Open Data."""

    stop_id: str
    name: str
    raw_name: str
    latitude: float | None = None
    longitude: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for compact JSON storage."""
        data: dict[str, Any] = {
            "n": self.name,
            "r": self.raw_name,
        }
        if self.latitude is not None:
            data["lat"] = self.latitude
        if self.longitude is not None:
            data["lon"] = self.longitude
        return data

    @classmethod
    def from_dict(cls, stop_id: str, data: dict[str, Any]) -> StopInfo:
        """Deserialize from compact JSON storage."""
        return cls(
            stop_id=stop_id,
            name=str(data.get("n") or data.get("r") or stop_id),
            raw_name=str(data.get("r") or data.get("n") or stop_id),
            latitude=data.get("lat"),
            longitude=data.get("lon"),
        )


@dataclass(frozen=True, slots=True)
class LineInfo:
    """Metadata for a TITSA bus line from Cabildo de Tenerife Open Data."""

    line: str
    name: str
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for compact JSON storage."""
        data: dict[str, Any] = {"n": self.name}
        if self.url:
            data["u"] = self.url
        return data

    @classmethod
    def from_dict(cls, line: str, data: dict[str, Any]) -> LineInfo:
        """Deserialize from compact JSON storage."""
        return cls(
            line=line,
            name=str(data.get("n") or ""),
            url=data.get("u"),
        )


@dataclass(slots=True)
class CatalogData:
    """Cached Open Data catalog (stops, lines, and compact GTFS stop→routes index)."""

    stops: dict[str, StopInfo] = field(default_factory=dict)
    lines: dict[str, LineInfo] = field(default_factory=dict)
    stop_routes: dict[str, list[str]] = field(default_factory=dict)
    csv_updated_at: str | None = None
    gtfs_updated_at: str | None = None
    gtfs_modified: str | None = None

    def to_storage_dict(self) -> dict[str, Any]:
        """Convert catalog into a compact JSON-serializable dictionary."""
        return {
            "stops": {sid: stop.to_dict() for sid, stop in self.stops.items()},
            "lines": {code: line.to_dict() for code, line in self.lines.items()},
            "stop_routes": self.stop_routes,
            "csv_updated_at": self.csv_updated_at,
            "gtfs_updated_at": self.gtfs_updated_at,
            "gtfs_modified": self.gtfs_modified,
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> CatalogData:
        """Restore catalog from stored JSON dictionary."""
        raw_stops = data.get("stops") or {}
        raw_lines = data.get("lines") or {}
        raw_stop_routes = data.get("stop_routes") or {}
        return cls(
            stops={
                str(sid): StopInfo.from_dict(str(sid), info)
                for sid, info in raw_stops.items()
                if isinstance(info, dict)
            },
            lines={
                str(code): LineInfo.from_dict(str(code), info)
                for code, info in raw_lines.items()
                if isinstance(info, dict)
            },
            stop_routes={
                str(sid): [str(r) for r in routes]
                for sid, routes in raw_stop_routes.items()
                if isinstance(routes, list)
            },
            csv_updated_at=data.get("csv_updated_at"),
            gtfs_updated_at=data.get("gtfs_updated_at"),
            gtfs_modified=data.get("gtfs_modified"),
        )
