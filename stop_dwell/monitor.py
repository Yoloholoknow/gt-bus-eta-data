"""Edit ROUTE_COLOR and VEHICLE_NUMBER, then run python3 -m stop_dwell.monitor."""

import math
import time
from dataclasses import dataclass

import requests

from combined_stop_visit_tracker import (
    ROUTES_URL,
    SOURCE_TIMESTAMP,
    STOPS_URL,
    VEHICLE_POINTS_URL,
    Observation,
    distance_meters,
    format_timestamp,
)


ROUTE_COLOR = "Gold"
VEHICLE_NUMBER = "2308"

STOP_RADIUS_METERS = 15
STOPPED_SPEED = 1.0  # GroundSpeed units, matching arrivals.py.
POLL_SECONDS = 3
MAX_FIX_GAP_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class Stop:
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Fix:
    vehicle_id: str
    source_ms: int
    latitude: float
    longitude: float
    speed: float

    @classmethod
    def from_point(cls, point: dict) -> "Fix":
        stamp = SOURCE_TIMESTAMP.fullmatch(str(point.get("TimeStamp")))
        if stamp is None or point.get("VehicleID") is None:
            raise ValueError("Missing GPS timestamp or vehicle ID")
        lat = float(point["Latitude"])
        lon = float(point["Longitude"])
        speed = float(point["GroundSpeed"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and math.isfinite(speed) and speed >= 0):
            raise ValueError("Invalid GPS coordinates or speed")
        return cls(str(point["VehicleID"]), int(stamp[1]), lat, lon, speed)

    def distance_to(self, stop: Stop) -> float:
        return distance_meters(self.latitude, self.longitude, stop.latitude, stop.longitude)


class DwellMonitor:
    def __init__(self, stops: list[Stop]):
        self.stops = stops
        self.last_fix: Fix | None = None
        self.last_fix_at = 0.0
        self.reset()

    def reset(self) -> None:
        # Keep the last fix so a cached repeat cannot restart tracking.
        self.continuous = False
        self.stop: Stop | None = None
        self.stopped_since = 0.0
        self.arrival_observed = False

    def update(self, fix: Fix, received_at: float) -> str:
        previous = self.last_fix
        if previous is not None and previous.vehicle_id == fix.vehicle_id:
            local_gap = received_at - self.last_fix_at
            source_gap = (fix.source_ms - previous.source_ms) / 1000
            if local_gap > MAX_FIX_GAP_SECONDS or source_gap > MAX_FIX_GAP_SECONDS:
                self.reset()
            if source_gap < 0 or (source_gap == 0 and fix != previous):
                self.reset()
                return "Status unknown (out-of-order or inconsistent GPS fix)"
            if source_gap == 0:
                if self.continuous:
                    return f"{self.status()} [last fresh GPS fix]"
                return "Status unknown (waiting for a fresh GPS fix)"
        else:
            self.reset()

        stop = self.stop_for(fix)
        if stop != self.stop:
            self.arrival_observed = self.continuous and self.stop is None and stop is not None
            self.stop = stop
            self.stopped_since = received_at

        self.last_fix = fix
        self.last_fix_at = received_at
        self.continuous = True
        return self.status()

    def stop_for(self, fix: Fix) -> Stop | None:
        if fix.speed >= STOPPED_SPEED:
            return None

        # Keep an active stop through small position changes near adjacent stops.
        if self.stop is not None and fix.distance_to(self.stop) <= STOP_RADIUS_METERS:
            return self.stop
        closest = min(self.stops, key=fix.distance_to)
        return closest if fix.distance_to(closest) <= STOP_RADIUS_METERS else None

    def status(self) -> str:
        if self.stop is None:
            return "Not stopped at a route stop"
        observed_seconds = int(self.last_fix_at - self.stopped_since)
        if self.arrival_observed:
            return f"At {self.stop.name} | stopped ~{observed_seconds}s (arrival observed)"
        return f"At {self.stop.name} | observed stopped for {observed_seconds}s (untracked arrival)"


def fetch_list(session: requests.Session, url: str, **params: str) -> list[dict]:
    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    items = response.json()
    if not isinstance(items, list):
        raise ValueError(f"{url.rsplit('/', 1)[-1]} did not return a list")
    return [item for item in items if isinstance(item, dict)]


def load_route(session: requests.Session) -> tuple[str, list[Stop]]:
    routes = {
        str(route["RouteID"]): str(route["Description"]).strip()
        for route in fetch_list(session, ROUTES_URL)
        if route.get("RouteID") is not None and route.get("Description")
    }
    matches = [rid for rid, name in routes.items() if name.casefold() == ROUTE_COLOR.strip().casefold()]
    if len(matches) != 1:
        raise ValueError(f"Expected one route for {ROUTE_COLOR!r}. Available routes: {', '.join(routes.values())}")
    route_id = matches[0]
    stops = []
    for item in fetch_list(session, STOPS_URL, routeID=route_id):
        if str(item.get("RouteID", route_id)) != route_id:
            continue
        try:
            latitude = float(item["Latitude"])
            longitude = float(item["Longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(item.get("Description") or item.get("Name") or "").strip()
        if name and -90 <= latitude <= 90 and -180 <= longitude <= 180:
            stops.append(Stop(name, latitude, longitude))
    if not stops:
        raise ValueError(f"No usable stops returned for {ROUTE_COLOR!r}")
    return route_id, stops


def run(session: requests.Session) -> None:
    route_id, stops = load_route(session)
    number = str(VEHICLE_NUMBER).strip()
    monitor = DwellMonitor(stops)
    print(f"Watching {ROUTE_COLOR} bus {number} (Ctrl+C to stop)", flush=True)
    while True:
        started_at = time.monotonic()
        try:
            points = fetch_list(session, VEHICLE_POINTS_URL)
            observed = Observation.now()
            matches = [
                point for point in points
                if str(point.get("Name", "")).strip() == number and str(point.get("RouteID")) == route_id
            ]
            if len(matches) != 1:
                monitor.reset()
                status = (
                    "Status unknown (multiple matching buses)"
                    if matches else "Bus not currently reporting on this route"
                )
            elif matches[0].get("IsOnRoute") is False:
                monitor.reset()
                status = "Status unknown (bus is off route)"
            else:
                status = monitor.update(Fix.from_point(matches[0]), observed.monotonic)
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            monitor.reset()
            observed = Observation.now()
            status = f"Status unknown (GPS data unavailable: {exc})"
        print(f"{format_timestamp(observed)} | {status}", flush=True)
        time.sleep(max(0, started_at + POLL_SECONDS - time.monotonic()))


def main() -> int:
    try:
        with requests.Session() as session:
            run(session)
    except KeyboardInterrupt:
        return 0
    except (requests.RequestException, ValueError) as exc:
        print(f"Could not start the monitor: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
