"""Collect observed IsArriving-to-geofence lead times without mixing API clocks."""

import argparse
import csv
import json
import logging
import math
import re
import sys
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, TextIO
from zoneinfo import ZoneInfo

import requests


# Leave empty to track every route. Names are case-insensitive.
ROUTE_NAME_FILTERS: list[str] = []
DEFAULT_GEOFENCE_METERS = 1.0
DEFAULT_POLL_SECONDS = 1.0
MAX_POLL_WORKERS = 4
MAX_OBSERVATION_GAP_INTERVALS = 3
GPS_MAX_UNCHANGED_SECONDS = 30.0
VISIT_TIMEOUT_SECONDS = 1800.0

BASE_URL = "https://bus.gatech.edu/Services/JSONPRelay.svc/"
ARRIVAL_TIMES_URL = BASE_URL + "GetStopArrivalTimes"
VEHICLE_POINTS_URL = BASE_URL + "GetMapVehiclePoints"
ROUTES_URL = BASE_URL + "GetRoutes"
STOPS_URL = BASE_URL + "GetStops"
EASTERN_TIME = ZoneInfo("America/New_York")
SOURCE_TIMESTAMP = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")
VisitKey = tuple[int, str, str]  # RouteID, RouteStopID, VehicleID

CSV_COLUMNS = [
    "route_color", "route_id", "route_stop_id", "stop_name", "vehicle_number",
    "is_arriving_time", "at_stop_time", "is_departing_time",
    "time_at_stop_seconds", "arriving_lead_seconds",
]


@dataclass(frozen=True)
class Observation:
    at: datetime
    monotonic: float

    @classmethod
    def now(cls) -> "Observation":
        return cls(datetime.now(timezone.utc), time.monotonic())


def format_timestamp(value: Observation | datetime | None) -> str:
    if value is None:
        return ""
    at = value.at if isinstance(value, Observation) else value
    return at.astimezone(EASTERN_TIME).isoformat(timespec="milliseconds")


def elapsed(end: Observation, start: Observation) -> float:
    return end.at.timestamp() - start.at.timestamp()


def clock_consistent(start: Observation, end: Observation) -> bool:
    return abs(elapsed(end, start) - (end.monotonic - start.monotonic)) <= 0.25


@dataclass
class StopConfig:
    name: str
    source_route_id: int
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class GPSFix:
    vehicle_id: str
    route_id: int
    latitude: float
    longitude: float
    observed: Observation
    source_order: int
    timestamp_raw: str
    seconds_raw: Any


@dataclass
class GeofenceState:
    current: GPSFix | None = None
    inside: bool | None = None
    outside: GPSFix | None = None
    entry: GPSFix | None = None
    entry_start: GPSFix | None = None
    started_inside: bool = False

    def update(self, fix: GPSFix, inside: bool) -> None:
        if self.current is not None and fix.observed.monotonic <= self.current.observed.monotonic:
            return
        self.current = fix
        if inside and self.inside is not True:
            self.entry = fix
            self.entry_start = self.outside
            self.started_inside = self.inside is None
        elif not inside:
            self.outside = fix
            self.entry = None
            self.entry_start = None
            self.started_inside = False
        self.inside = inside


@dataclass
class ActiveVisit:
    visit_id: str
    key: VisitKey
    signal: Observation
    signal_start: Observation | None
    arrival_request_seconds: float
    arrival_poll_gap_seconds: float | None
    entry: GPSFix | None = None
    entry_start: GPSFix | None = None
    departure: Observation | None = None
    flags: set[str] = field(default_factory=set)


@dataclass
class PairState:
    key: VisitKey
    flag: bool | None = None
    last_signal: Observation | None = None
    last_false: Observation | None = None
    geo: GeofenceState = field(default_factory=GeofenceState)
    armed: bool = False
    # Keep a finished/excluded visit latched until false + outside is seen.
    latched: bool = False
    visit: ActiveVisit | None = None


@dataclass
class PollStream:
    name: str
    kind: str
    url: str
    params: dict[str, str] = field(default_factory=dict)
    stop_ids: tuple[str, ...] = ()
    session: requests.Session | None = None
    previous_start: float | None = None
    next_due: float = 0.0


@dataclass
class PollResult:
    stream: PollStream
    started: Observation
    received: Observation
    duration: float
    start_gap: float | None = None
    data: Any = None
    error: str | None = None
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    response_text: str | None = None


class Diagnostics:
    def __init__(self, output: TextIO, run_id: str):
        self.output = output
        self.run_id = run_id

    def write(self, kind: str, **details: Any) -> None:
        self.output.write(json.dumps(
            {"kind": kind, "run_id": self.run_id,
             "logged_at": format_timestamp(Observation.now()), **details},
            ensure_ascii=False,
        ) + "\n")
        self.output.flush()

    def response(self, result: PollResult) -> None:
        self.write(
            "request", stream=result.stream.name, url=result.stream.url,
            params=result.stream.params, stop_ids=result.stream.stop_ids,
            request_started_at=format_timestamp(result.started),
            response_received_at=format_timestamp(result.received),
            request_seconds=result.duration, poll_gap_seconds=result.start_gap,
            processing_delay_seconds=time.monotonic() - result.received.monotonic,
            status_code=result.status_code, headers=result.headers,
            error=result.error, data=result.data, response_text=result.response_text,
        )


def perform_request(stream: PollStream, timeout_seconds: float) -> PollResult:
    # The scheduler allows only one outstanding request per stream. Each stream
    # owns a Session; no Session is used by concurrent requests.
    if stream.session is None:
        stream.session = requests.Session()
    started = Observation.now()
    gap = None if stream.previous_start is None else started.monotonic - stream.previous_start
    stream.previous_start = started.monotonic
    result = PollResult(stream, started, started, 0.0, start_gap=gap)
    try:
        response = stream.session.get(stream.url, params=stream.params, timeout=timeout_seconds)
        result.received = Observation.now()  # requests has received the full body.
        result.status_code = response.status_code
        result.headers = {k: response.headers[k] for k in ("Date", "Age", "Cache-Control", "ETag")
                          if k in response.headers}
        result.response_text = response.text
        try:
            result.data = response.json()
            result.response_text = None
        except ValueError:
            result.error = "Response is not valid JSON"
        response.raise_for_status()
    except requests.RequestException as exc:
        if result.status_code is None:
            result.received = Observation.now()
        result.error = f"{type(exc).__name__}: {exc}"
    result.duration = result.received.monotonic - started.monotonic
    return result


def load_stop_config(path: str) -> dict[str, StopConfig]:
    with open(path, encoding="utf-8") as source:
        raw = json.load(source)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Config must be a non-empty object keyed by RouteStopID.")
    stops = {}
    for stop_id, item in raw.items():
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            raise ValueError(f"Missing stop name for {stop_id}.")
        if item.get("source_route_id") is None:
            raise ValueError(f"Missing source_route_id for stop {stop_id}.")
        stops[str(stop_id)] = StopConfig(str(item["name"]), int(item["source_route_id"]))
    return stops


def ensure_csv_header(path: str) -> None:
    target = Path(path)
    if target.exists() and target.stat().st_size:
        with target.open(newline="", encoding="utf-8") as source:
            if next(csv.reader(source), None) != CSV_COLUMNS:
                raise ValueError(f"Incompatible CSV header in {path}. Choose a new --output path.")
        return
    with target.open("a", newline="", encoding="utf-8") as output:
        csv.DictWriter(output, fieldnames=CSV_COLUMNS).writeheader()
        output.flush()


def prepare_stops(
    stops: dict[str, StopConfig], timeout: float, diagnostics: Diagnostics,
) -> tuple[dict[str, StopConfig], dict[int, str]]:
    def fetch(session: requests.Session, name: str, url: str, params: dict[str, str]) -> list:
        stream = PollStream(name, "metadata", url, params, session=session)
        result = perform_request(stream, timeout)
        diagnostics.response(result)
        if result.error or not isinstance(result.data, list):
            raise ValueError(f"{name}: {result.error or 'Expected a JSON list'}")
        return result.data

    with requests.Session() as session:
        routes = fetch(session, "routes", ROUTES_URL, {})
        names = {int(r["RouteID"]): str(r["Description"]).strip()
                 for r in routes if isinstance(r, dict) and r.get("RouteID") is not None
                 and r.get("Description")}
        filters = {name.strip().casefold() for name in ROUTE_NAME_FILTERS if name.strip()}
        if filters:
            unmatched = filters - {name.casefold() for name in names.values()}
            if unmatched:
                raise ValueError("Unknown route names: " + ", ".join(sorted(unmatched)))
            stops = {sid: s for sid, s in stops.items()
                     if names.get(s.source_route_id, "").casefold() in filters}
        if not stops:
            raise ValueError("The selected routes have no configured stops.")

        def load_coordinates(items: Any) -> None:
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("RouteStopID", item.get("RouteStopId", "")))
                if sid not in stops:
                    continue
                try:
                    lat, lon = float(item["Latitude"]), float(item["Longitude"])
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        continue
                    stops[sid].latitude, stops[sid].longitude = lat, lon
                except (KeyError, TypeError, ValueError):
                    continue

        for route in routes:
            if isinstance(route, dict):
                load_coordinates(route.get("Stops"))
        for rid in sorted({s.source_route_id for s in stops.values() if s.latitude is None}):
            load_coordinates(fetch(session, f"stops:{rid}", STOPS_URL, {"routeID": str(rid)}))
    missing = [sid for sid, s in stops.items() if s.latitude is None or s.longitude is None]
    if missing:
        raise ValueError("No coordinates returned for RouteStopIDs: " + ", ".join(missing))
    return stops, names


def distance_meters(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    a, b = math.radians(lat_a), math.radians(lat_b)
    h = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(math.radians(lon_b - lon_a) / 2) ** 2
    return 12_742_000 * math.asin(math.sqrt(min(1.0, max(0.0, h))))


class VisitTracker:
    """All visit state and all file writes belong to the single event consumer."""

    def __init__(
        self, stops: dict[str, StopConfig], route_names: dict[int, str], radius: float,
        interval: float, output: TextIO, diagnostics: Diagnostics,
        emit: Callable[..., None] = print,
    ):
        self.stops = stops
        self.route_names = route_names
        self.radius = radius
        self.max_gap = MAX_OBSERVATION_GAP_INTERVALS * interval
        self.output = output
        self.writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
        self.diagnostics = diagnostics
        self.emit = emit
        self.pairs: dict[VisitKey, PairState] = {}
        self.fixes: dict[str, GPSFix] = {}
        self.vehicle_numbers: dict[str, str] = {}
        self.successes: dict[str, Observation] = {}
        self.streams: dict[str, PollStream] = {}
        self.gapped_streams: set[str] = set()
        self.invalid_gps: set[str] = set()
        self.sequence = 0

    def log(self, tag: str, pair: PairState, **fields: Any) -> None:
        rid, sid, vid = pair.key
        parts = [f"[{tag}]", f"vehicle={self.vehicle_numbers.get(vid, vid)}",
                 f"route={self.route_names.get(rid, rid)}", f"route_stop_id={sid}",
                 f"stop={self.stops[sid].name}"]
        parts.extend(f"{key}={value}" for key, value in fields.items())
        self.emit(" ".join(parts), flush=True)

    def flag(self, pair: PairState, reason: str, force: bool = False) -> None:
        visit = pair.visit
        if visit is None or (visit.entry is not None and not force) or reason in visit.flags:
            return
        visit.flags.add(reason)
        self.log("excluded", pair, reason=reason, visit_id=visit.visit_id)
        self.diagnostics.write("quality", visit_id=visit.visit_id, reason=reason)

    def invalidate_signal(self, stop_ids: tuple[str, ...], reason: str) -> None:
        for pair in self.pairs.values():
            if pair.key[1] in stop_ids:
                self.flag(pair, reason)
                pair.flag = None
                pair.last_false = None
                pair.armed = False

    def invalidate_gps(self, vehicle_id: str, reason: str) -> None:
        self.invalid_gps.add(vehicle_id)
        for pair in self.pairs.values():
            if pair.key[2] == vehicle_id:
                self.flag(pair, reason)
                pair.geo = GeofenceState()
                pair.armed = False

    def stream_problem(self, stream: PollStream, reason: str) -> None:
        if stream.kind == "arrivals":
            self.invalidate_signal(stream.stop_ids, reason)
        else:
            for vid in {key[2] for key in self.pairs} | self.fixes.keys():
                self.invalidate_gps(vid, reason)

    def handle(self, result: PollResult) -> None:
        self.diagnostics.response(result)
        stream, observed = result.stream, result.received
        self.streams[stream.name] = stream
        previous = self.successes.get(stream.name)
        if previous is not None:
            if observed.monotonic <= previous.monotonic:
                self.stream_problem(stream, "out_of_order_response")
                return
            if not clock_consistent(previous, observed):
                self.stream_problem(stream, "clock_changed")
            if observed.monotonic - previous.monotonic > self.max_gap:
                self.stream_problem(stream, "observation_gap")
        if result.error or not isinstance(result.data, list):
            self.stream_problem(stream, "request_failed")
            logging.warning("%s failed: %s", stream.name, result.error or "Expected a JSON list")
            return
        self.successes[stream.name] = observed
        self.gapped_streams.discard(stream.name)
        if stream.kind == "arrivals":
            self.arrivals(result)
        elif stream.kind == "gps":
            self.gps(result)

    def pair_for(self, key: VisitKey, observed: Observation) -> PairState:
        if key not in self.pairs:
            pair = self.pairs[key] = PairState(key)
            fix = self.fixes.get(key[2])
            if (fix and fix.route_id == key[0] and key[2] not in self.invalid_gps
                    and 0 <= observed.monotonic - fix.observed.monotonic <= GPS_MAX_UNCHANGED_SECONDS):
                pair.geo.update(fix, self.inside(key[1], fix))
        return self.pairs[key]

    def inside(self, sid: str, fix: GPSFix) -> bool:
        stop = self.stops[sid]
        return distance_meters(fix.latitude, fix.longitude, stop.latitude, stop.longitude) <= self.radius

    def arm(self, pair: PairState, now: Observation) -> None:
        outside, last_false = pair.geo.outside, pair.last_false
        pair.armed = bool(
            pair.visit is None and pair.flag is False and pair.geo.inside is False
            and outside is not None and last_false is not None
            and pair.key[2] not in self.invalid_gps
            and 0 <= now.monotonic - outside.observed.monotonic <= GPS_MAX_UNCHANGED_SECONDS
            and 0 <= now.monotonic - last_false.monotonic <= self.max_gap
        )
        if pair.armed:
            pair.latched = False

    def arrivals(self, result: PollResult) -> None:
        observed = result.received
        seen_stops: set[str] = set()
        for item in result.data:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("RouteStopId", item.get("RouteStopID", "")))
            if sid not in result.stream.stop_ids or sid not in self.stops:
                continue
            try:
                rid = int(item["RouteId"])
            except (KeyError, TypeError, ValueError):
                continue
            if rid != self.stops[sid].source_route_id or not isinstance(item.get("Times"), list):
                continue
            seen_stops.add(sid)
            # A bus may have multiple predictions for different laps. A current
            # arriving prediction takes priority over its later false entries.
            values: dict[str, list[dict]] = {}
            complete = True
            for entry in item["Times"]:
                if not isinstance(entry, dict):
                    complete = False
                    continue
                if entry.get("VehicleId") is None:
                    continue  # Scheduled entries are not a tracked vehicle.
                values.setdefault(str(entry["VehicleId"]), []).append(entry)
            seen_keys = set()
            for vid, entries in values.items():
                key = (rid, sid, vid)
                seen_keys.add(key)
                pair = self.pair_for(key, observed)
                if any(e.get("IsArriving") is True and e.get("IsDeparted") is not True for e in entries):
                    flag = True
                elif any(e.get("IsArriving") is False for e in entries):
                    flag = False
                elif any(e.get("IsDeparted") is True for e in entries):
                    if pair.visit is not None:
                        pair.visit.departure = observed
                    self.finish_visit(pair, "departed")
                    pair.flag, pair.last_false, pair.armed = None, None, False
                    continue
                else:
                    self.flag(pair, "malformed_signal")
                    pair.flag, pair.last_false, pair.armed = None, None, False
                    continue
                if (pair.last_signal is not None
                        and observed.monotonic - pair.last_signal.monotonic > self.max_gap):
                    self.flag(pair, "observation_gap")
                    pair.last_false, pair.armed = None, False
                if flag:
                    if pair.visit is None and not pair.latched:
                        self.start_visit(pair, result)
                    pair.flag = True
                else:
                    if pair.visit is not None:
                        pair.visit.departure = observed
                    self.finish_visit(pair, "signal_false")
                    pair.flag = False
                    pair.last_false = observed
                    self.arm(pair, observed)
                pair.last_signal = observed
            # An omitted vehicle ends its current prediction episode but never
            # counts as an observed false signal for arming the next visit.
            for key, pair in list(self.pairs.items()):
                if key[1] != sid or key in seen_keys:
                    continue
                if complete:
                    self.finish_visit(pair, "prediction_disappeared")
                else:
                    self.flag(pair, "malformed_signal")
                pair.flag, pair.last_false, pair.armed = None, None, False
        for sid in set(result.stream.stop_ids) - seen_stops:
            self.invalidate_signal((sid,), "missing_stop_response")
        self.check_ambiguity()
        self.write_arrivals()

    def start_visit(self, pair: PairState, result: PollResult) -> None:
        self.sequence += 1
        pair.visit = ActiveVisit(
            f"{self.diagnostics.run_id}:{self.sequence}", pair.key, result.received,
            pair.last_false, result.duration, result.start_gap,
        )
        pair.latched = True
        self.log("arriving", pair, time=format_timestamp(result.received),
                 logged_at=format_timestamp(Observation.now()),
                 request_seconds=f"{result.duration:.3f}",
                 poll_gap_seconds="unknown" if result.start_gap is None else f"{result.start_gap:.3f}")
        if pair.last_false is None:
            self.flag(pair, "startup_arriving")
        if not pair.armed:
            self.flag(pair, "missing_outside_baseline")
        if pair.geo.inside is True and pair.geo.entry:
            if pair.geo.started_inside:
                self.flag(pair, "startup_inside")
            self.capture_entry(pair, pair.geo.entry, pair.geo.entry_start)
        pair.armed = False

    def capture_entry(self, pair: PairState, fix: GPSFix, outside: GPSFix | None) -> None:
        visit = pair.visit
        if visit is None or visit.entry is not None:
            return
        visit.entry, visit.entry_start = fix, outside
        if outside is None:
            self.flag(pair, "entry_unbracketed", force=True)
        elif outside.observed.monotonic >= fix.observed.monotonic:
            self.flag(pair, "invalid_entry_bracket", force=True)
        if fix.observed.monotonic < visit.signal.monotonic:
            self.flag(pair, "entry_before_signal", force=True)
        elif outside is not None and outside.observed.monotonic < visit.signal.monotonic:
            self.flag(pair, "timing_overlap", force=True)
        if not clock_consistent(visit.signal, fix.observed):
            self.flag(pair, "clock_changed", force=True)
        self.log("at_stop", pair, time=format_timestamp(fix.observed), radius=f"{self.radius:g}m",
                 quality=";".join(sorted(visit.flags)) or "pending")

    def write_arrivals(self) -> None:
        """Keep visits open until departure is observed."""
        return

    def gps(self, result: PollResult) -> None:
        observed = result.received
        seen: set[str] = set()
        for item in result.data:
            if not isinstance(item, dict) or item.get("VehicleID") is None:
                continue
            vid = str(item["VehicleID"])
            seen.add(vid)
            if item.get("Name"):
                self.vehicle_numbers[vid] = str(item["Name"])
            raw_timestamp = item.get("TimeStamp")
            match = SOURCE_TIMESTAMP.fullmatch(raw_timestamp) if isinstance(raw_timestamp, str) else None
            if match is None:
                self.invalidate_gps(vid, "missing_gps_timestamp")
                continue
            try:
                lat, lon, rid = float(item["Latitude"]), float(item["Longitude"]), int(item["RouteID"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Invalid coordinates")
            except (KeyError, TypeError, ValueError):
                self.invalidate_gps(vid, "invalid_gps")
                continue
            if item.get("IsOnRoute") is False:
                self.invalidate_gps(vid, "off_route")
                continue
            fix = GPSFix(vid, rid, lat, lon, observed, int(match[1]), raw_timestamp, item.get("Seconds"))
            previous = self.fixes.get(vid)
            if previous is not None:
                if fix.source_order < previous.source_order:
                    self.invalidate_gps(vid, "out_of_order_gps")
                    continue
                if fix.source_order == previous.source_order:
                    if (lat, lon, rid) != (previous.latitude, previous.longitude, previous.route_id):
                        self.invalidate_gps(vid, "inconsistent_gps")
                    # Seconds may count down on an identical fix. Never move
                    # its observation time or manufacture a newer GPS sample.
                    continue
                if not clock_consistent(previous.observed, observed):
                    self.invalidate_gps(vid, "clock_changed")
                if rid != previous.route_id:
                    self.invalidate_gps(vid, "route_changed")
                    for pair in self.pairs.values():
                        if pair.key[2] == vid:
                            self.finish_visit(pair, "route_changed")
                            pair.flag, pair.last_false, pair.armed = None, None, False
            self.fixes[vid] = fix
            self.invalid_gps.discard(vid)
            for pair in self.pairs.values():
                if pair.key[0] != rid or pair.key[2] != vid:
                    continue
                pair.geo.update(fix, self.inside(pair.key[1], fix))
                if pair.geo.inside is True and pair.geo.entry:
                    if pair.geo.started_inside:
                        self.flag(pair, "startup_inside")
                    self.capture_entry(pair, pair.geo.entry, pair.geo.entry_start)
                self.arm(pair, observed)
        for vid in {key[2] for key in self.pairs} - seen:
            self.invalidate_gps(vid, "missing_vehicle_response")
        self.write_arrivals()

    def check_ambiguity(self) -> None:
        active = [p for p in self.pairs.values() if p.visit is not None and p.flag is True]
        for i, first in enumerate(active):
            for second in active[i + 1:]:
                if first.key[0] != second.key[0] or first.key[2] != second.key[2]:
                    continue
                a, b = self.stops[first.key[1]], self.stops[second.key[1]]
                if distance_meters(a.latitude, a.longitude, b.latitude, b.longitude) <= 2 * self.radius:
                    self.flag(first, "ambiguous_stop", force=True)
                    self.flag(second, "ambiguous_stop", force=True)

    def finish_visit(self, pair: PairState, reason: str) -> None:
        visit = pair.visit
        if visit is None:
            return
        if visit.entry is None:
            visit.flags.add("missing_entry")
        if visit.departure is None:
            visit.flags.add("missing_departure")
        if visit.signal_start is None:
            visit.flags.add("signal_unbracketed")
        signal_width = entry_width = None
        if visit.signal_start is not None:
            signal_width = elapsed(visit.signal, visit.signal_start)
            if signal_width < 0 or not clock_consistent(visit.signal_start, visit.signal):
                visit.flags.add("invalid_signal_bracket")
        if visit.entry is not None and visit.entry_start is not None:
            entry_width = elapsed(visit.entry.observed, visit.entry_start.observed)
            if entry_width <= 0 or not clock_consistent(visit.entry_start.observed, visit.entry.observed):
                visit.flags.add("invalid_entry_bracket")
        elif visit.entry is not None:
            visit.flags.add("entry_unbracketed")
        lead = lower = upper = None
        if not visit.flags:
            lead = elapsed(visit.entry.observed, visit.signal)
            lower = elapsed(visit.entry_start.observed, visit.signal)
            upper = elapsed(visit.entry.observed, visit.signal_start)
            if (lead < 0 or lower < 0 or upper < lead
                    or visit.departure is None
                    or elapsed(visit.departure, visit.entry.observed) < 0):
                visit.flags.add("invalid_event_order")
                lead = lower = upper = None
        def number(value: float | None) -> float | str:
            return "" if value is None else round(value, 3)
        rid, sid, vid = pair.key
        quality = ";".join(sorted(visit.flags)) or "ok"
        # The CSV is the clean analysis dataset: only complete, validated
        # measurements belong there. Keep rejected visits in JSONL diagnostics.
        row = None
        if (not visit.flags and lead is not None and upper is not None and lower is not None
                and visit.entry is not None and visit.departure is not None):
            row = {
                "route_color": self.route_names.get(rid, str(rid)),
                "route_id": rid,
                "route_stop_id": sid,
                "stop_name": self.stops[sid].name,
                "vehicle_number": self.vehicle_numbers.get(vid, vid),
                "is_arriving_time": format_timestamp(visit.signal),
                "at_stop_time": format_timestamp(visit.entry.observed),
                "is_departing_time": format_timestamp(visit.departure),
                "time_at_stop_seconds": number(elapsed(visit.departure, visit.entry.observed)),
                "arriving_lead_seconds": number(lead),
            }
            self.writer.writerow(row)
            self.output.flush()
        pair.visit = None
        pair.armed = False
        self.diagnostics.write(
            "visit", visit_id=visit.visit_id, route_id=rid, route_stop_id=sid,
            vehicle_id=vid, isarriving_time=format_timestamp(visit.signal),
            at_stop_time=format_timestamp(visit.entry.observed if visit.entry else None),
            is_departing_time=format_timestamp(visit.departure),
            time_at_stop_seconds=number(elapsed(visit.departure, visit.entry.observed)
                if visit.departure is not None and visit.entry is not None else None),
            lead_seconds=number(lead), lead_uncertainty_seconds=number(
                upper - lower if upper is not None and lower is not None else None
            ), quality=quality, csv_written=row is not None, end=reason,
        )
        self.log("lead", pair, lead="unknown" if lead is None else f"{lead:.3f}s",
                 quality=quality, csv_written=row is not None, end=reason)
        # Leave the latch set until fresh false + outside observations rearm it.

    def tick(self, now: Observation) -> None:
        for name, previous in self.successes.items():
            if now.monotonic - previous.monotonic > self.max_gap and name not in self.gapped_streams:
                self.stream_problem(self.streams[name], "observation_gap")
                self.gapped_streams.add(name)
        for vid, fix in self.fixes.items():
            if now.monotonic - fix.observed.monotonic > GPS_MAX_UNCHANGED_SECONDS and vid not in self.invalid_gps:
                self.invalidate_gps(vid, "stale_gps")
        for pair in self.pairs.values():
            if pair.visit and now.monotonic - pair.visit.signal.monotonic > VISIT_TIMEOUT_SECONDS:
                self.flag(pair, "visit_timeout")
                self.finish_visit(pair, "visit_timeout")

    def finish(self, reason: str) -> None:
        for pair in self.pairs.values():
            if pair.visit and (pair.visit.entry is None or pair.visit.departure is None):
                pair.visit.flags.add("incomplete_visit")
            self.finish_visit(pair, reason)


def run_polling(
    streams: list[PollStream], tracker: VisitTracker, interval: float, timeout: float,
    duration: float | None = None,
    request: Callable[[PollStream, float], PollResult] = perform_request,
) -> None:
    """Bounded scheduling, no overlapping request per stream, no catch-up queue."""
    completed: Queue[tuple[PollStream, Future]] = Queue()
    outstanding: dict[str, Future] = {}
    started = time.monotonic()
    end_reason = "duration_complete"
    executor = ThreadPoolExecutor(max_workers=MAX_POLL_WORKERS, thread_name_prefix="transloc")

    def accept(stream: PollStream, future: Future) -> None:
        outstanding.pop(stream.name, None)
        result = future.result()
        tracker.handle(result)
        now = time.monotonic()
        # Advance past missed deadlines; a slow request never causes a burst.
        slots = math.floor((now - result.started.monotonic) / interval) + 1
        stream.next_due = result.started.monotonic + slots * interval

    try:
        while duration is None or time.monotonic() - started < duration:
            now = time.monotonic()
            tracker.tick(Observation.now())
            due = sorted((s for s in streams if s.name not in outstanding and s.next_due <= now),
                         key=lambda s: s.next_due)
            for stream in due[:MAX_POLL_WORKERS - len(outstanding)]:
                future = executor.submit(request, stream, timeout)
                outstanding[stream.name] = future
                future.add_done_callback(lambda f, s=stream: completed.put((s, f)))
            deadlines = [s.next_due for s in streams if s.name not in outstanding]
            wait_seconds = min(0.1, max(0.001, min(deadlines) - time.monotonic())) if deadlines else 0.1
            try:
                stream, future = completed.get(timeout=wait_seconds)
                accept(stream, future)
            except Empty:
                pass
    except KeyboardInterrupt:
        end_reason = "interrupted"
    except Exception:
        end_reason = "collector_error"
        raise
    finally:
        executor.shutdown(wait=True)
        # Retain the final in-flight responses as diagnostics without starting
        # any more requests. Finish pending visits exactly once.
        while not completed.empty():
            stream, future = completed.get_nowait()
            if end_reason == "collector_error":
                if not future.cancelled() and future.exception() is None:
                    tracker.diagnostics.response(future.result())
            else:
                accept(stream, future)
        tracker.finish(end_reason)
        for stream in streams:
            if stream.session is not None:
                stream.session.close()


def run_tracker(
    config_path: str, output_path: str, poll_seconds: float, request_timeout_seconds: float,
    chunk_size: int, geofence_radius_meters: float, diagnostics_path: str | None = None,
    duration_seconds: float | None = None,
) -> None:
    for name, value in (("Poll interval", poll_seconds), ("Request timeout", request_timeout_seconds),
                        ("Geofence radius", geofence_radius_meters)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be positive and finite.")
    if chunk_size <= 0:
        raise ValueError("Chunk size must be positive.")
    if duration_seconds is not None and (not math.isfinite(duration_seconds) or duration_seconds <= 0):
        raise ValueError("Duration must be positive and finite.")
    diagnostics_path = diagnostics_path or str(Path(output_path).with_suffix(".jsonl"))
    paths = [Path(p).resolve() for p in (config_path, output_path, diagnostics_path)]
    if len(set(paths)) != len(paths):
        raise ValueError("Config, CSV output, and diagnostics must use different paths.")
    stops = load_stop_config(config_path)
    ensure_csv_header(output_path)
    run_id = uuid.uuid4().hex[:12]
    with open(diagnostics_path, "a", encoding="utf-8") as raw:
        diagnostics = Diagnostics(raw, run_id)
        stops, names = prepare_stops(stops, request_timeout_seconds, diagnostics)
        ids = list(stops)
        streams = [PollStream(
            f"arrivals:{i // chunk_size}", "arrivals", ARRIVAL_TIMES_URL,
            {"routeStopIDs": ",".join(ids[i:i + chunk_size])}, tuple(ids[i:i + chunk_size]),
        ) for i in range(0, len(ids), chunk_size)]
        streams.append(PollStream("gps", "gps", VEHICLE_POINTS_URL))
        diagnostics.write("run_start", schema_version=2, poll_seconds=poll_seconds,
                          radius_meters=geofence_radius_meters, route_filters=ROUTE_NAME_FILTERS,
                          stops={sid: vars(stop) for sid, stop in stops.items()},
                          time_basis="response_received")
        with open(output_path, "a", newline="", encoding="utf-8") as output:
            tracker = VisitTracker(stops, names, geofence_radius_meters, poll_seconds, output, diagnostics)
            print(f"Tracking {len(stops)} route-stops; target interval={poll_seconds:g}s; "
                  f"radius={geofence_radius_meters:g}m; routes={', '.join(ROUTE_NAME_FILTERS) or 'all'}; "
                  f"CSV={output_path}; diagnostics={diagnostics_path}", flush=True)
            run_polling(streams, tracker, poll_seconds, request_timeout_seconds, duration_seconds)
        diagnostics.write("run_end")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track arrival, 10-meter stop entry, and departure for configured route-stops.")
    parser.add_argument("--config", default="all_stops.json", help="Route-stop config JSON.")
    parser.add_argument("--output", default="combined_stop_visits.csv", help="Append-only CSV of completed valid stop visits.")
    parser.add_argument("--diagnostics", help="Raw JSONL path (defaults to the CSV path with .jsonl suffix).")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS,
                        help="Target request-start interval per stream, in seconds.")
    parser.add_argument("--request-timeout-seconds", type=float, default=10, help="HTTP request timeout.")
    parser.add_argument("--chunk-size", type=int, default=50, help="RouteStopIDs per arrivals request.")
    parser.add_argument("--geofence-radius-meters", type=float, default=10.0, help="GPS radius used to mark at_stop_time, in meters (default: 10).")
    parser.add_argument("--duration-seconds", type=float, help="Optional bounded collection duration after startup.")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    try:
        run_tracker(args.config, args.output, args.poll_seconds, args.request_timeout_seconds,
                    args.chunk_size, args.geofence_radius_meters, args.diagnostics, args.duration_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
