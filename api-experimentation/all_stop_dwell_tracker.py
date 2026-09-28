import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests



# This all-stops tracker uses the same TransLoc arrival signal as the known-hold
# tracker, but watches every RouteStopID listed in all_stops.json.
ARRIVAL_TIMES_URL = "https://bus.gatech.edu/Services/JSONPRelay.svc/GetStopArrivalTimes"
VEHICLE_POINTS_URL = "https://bus.gatech.edu/Services/JSONPRelay.svc/GetMapVehiclePoints"
EASTERN_TIME = ZoneInfo("America/New_York")

# Measured dwell only. Unlike known_hold_dwell_tracker.py, this file does not
# compare against an expected dwell because normal stops do not have one yet.
CSV_COLUMNS = [
    "route_color",
    "route_id",
    "route_stop_id",
    "stop_name",
    "vehicle_number",
    "arrival_time",
    "departure_time",
    "actual_dwell_seconds",
]


@dataclass
class StopConfig:
    # source_route_id comes from response.json and is helpful when reviewing
    # duplicated stop names across routes.
    name: str
    source_route_id: int | None


@dataclass
class ActiveVisit:
    # Stored after arrival detection and removed once departure is detected.
    route_color: str
    route_id: Any
    route_stop_id: str
    stop_name: str
    vehicle_number: str
    arrival_time: datetime


def eastern_now() -> datetime:
    # Keep timestamps in Georgia Tech local time for easier field analysis.
    return datetime.now(EASTERN_TIME)


def format_timestamp(timestamp: datetime) -> str:
    return timestamp.isoformat(timespec="seconds")


def load_stop_config(path: str) -> dict[str, StopConfig]:
    # all_stops.json is generated from response.json and keyed by RouteStopID.
    with open(path, "r", encoding="utf-8") as config_file:
        raw_config = json.load(config_file)

    if not isinstance(raw_config, dict) or not raw_config:
        raise ValueError("Config must be a non-empty JSON object keyed by RouteStopID.")

    stop_config: dict[str, StopConfig] = {}
    for route_stop_id, stop_data in raw_config.items():
        route_stop_id = str(route_stop_id).strip()
        if not route_stop_id:
            raise ValueError("Config contains an empty RouteStopID key.")
        if not isinstance(stop_data, dict):
            raise ValueError(f"Config entry for {route_stop_id} must be an object.")

        name = str(stop_data.get("name", "")).strip()
        if not name:
            raise ValueError(f"Config entry for {route_stop_id} must include a non-empty name.")

        source_route_id = stop_data.get("source_route_id")
        if source_route_id is not None:
            source_route_id = int(source_route_id)

        stop_config[route_stop_id] = StopConfig(
            name=name,
            source_route_id=source_route_id,
        )

    return stop_config


def ensure_csv_header(path: str) -> None:
    # Long runs append to the same file; write the header only for a new/empty CSV.
    needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
    if not needs_header:
        return

    with open(path, "a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        csv_file.flush()


def chunks(items: list[str], chunk_size: int) -> list[list[str]]:
    # Avoid one oversized URL when tracking every stop in the system.
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]


def fetch_arrival_times(
    session: requests.Session,
    route_stop_ids: list[str],
    chunk_size: int,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    # Each chunk is a separate GetStopArrivalTimes call; the results are merged
    # into one list before state processing.
    all_results: list[dict[str, Any]] = []
    for route_stop_id_chunk in chunks(route_stop_ids, chunk_size):
        response = session.get(
            ARRIVAL_TIMES_URL,
            params={"routeStopIDs": ",".join(route_stop_id_chunk)},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("Expected arrival response to be a list.")
        all_results.extend(data)
    return all_results


def fetch_vehicle_numbers(
    session: requests.Session,
    timeout_seconds: int,
) -> dict[str, str]:
    # Arrival responses use internal VehicleId values. Vehicle points map them
    # to public bus numbers, stored in the Name field.
    response = session.get(VEHICLE_POINTS_URL, timeout=timeout_seconds)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("Expected vehicle response to be a list.")

    vehicle_numbers: dict[str, str] = {}
    for vehicle in data:
        if not isinstance(vehicle, dict):
            continue
        vehicle_id = vehicle.get("VehicleID")
        vehicle_number = vehicle.get("Name")
        if vehicle_id is not None and vehicle_number:
            vehicle_numbers[str(vehicle_id)] = str(vehicle_number)
    return vehicle_numbers


def write_completed_visit(
    csv_writer: csv.DictWriter,
    csv_file: Any,
    visit: ActiveVisit,
    departure_time: datetime,
) -> None:
    # Write only completed stop visits so each CSV row has a measurable dwell.
    actual_dwell_seconds = round((departure_time - visit.arrival_time).total_seconds(), 1)

    csv_writer.writerow(
        {
            "route_color": visit.route_color,
            "route_id": visit.route_id,
            "route_stop_id": visit.route_stop_id,
            "stop_name": visit.stop_name,
            "vehicle_number": visit.vehicle_number,
            "arrival_time": format_timestamp(visit.arrival_time),
            "departure_time": format_timestamp(departure_time),
            "actual_dwell_seconds": actual_dwell_seconds,
        }
    )
    csv_file.flush()

    print(
        "[departure] "
        f"vehicle={visit.vehicle_number} route={visit.route_color} stop={visit.stop_name} "
        f"route_stop_id={visit.route_stop_id} dwell={actual_dwell_seconds}s",
        flush=True,
    )


def process_successful_poll(
    stop_results: list[dict[str, Any]],
    stop_config: dict[str, StopConfig],
    active_visits: dict[tuple[str, str], ActiveVisit],
    vehicle_numbers: dict[str, str],
    csv_writer: csv.DictWriter,
    csv_file: Any,
    poll_time: datetime,
) -> None:
    # Active visits are keyed by route-stop and internal vehicle ID. The public
    # bus number is only a display field and can change/fail to resolve.
    seen_vehicle_stops: set[tuple[str, str]] = set()
    departed_keys: set[tuple[str, str]] = set()

    for stop_result in stop_results:
        route_stop_id = str(stop_result.get("RouteStopId", ""))
        if route_stop_id not in stop_config:
            continue

        route_id = stop_result.get("RouteId", "")
        route_color = str(
            stop_result.get("RouteDescription")
            or stop_result.get("Color")
            or route_id
            or ""
        )
        configured_stop = stop_config[route_stop_id]
        times = stop_result.get("Times", [])
        if not isinstance(times, list):
            logging.warning("Skipping malformed Times for RouteStopID %s", route_stop_id)
            continue

        for arrival_time in times:
            if not isinstance(arrival_time, dict):
                logging.warning("Skipping malformed arrival entry for RouteStopID %s", route_stop_id)
                continue

            vehicle_id = arrival_time.get("VehicleId")
            if vehicle_id is None:
                logging.warning("Skipping arrival entry without VehicleId for RouteStopID %s", route_stop_id)
                continue

            vehicle_id = str(vehicle_id)
            vehicle_number = vehicle_numbers.get(vehicle_id, vehicle_id)
            visit_key = (route_stop_id, vehicle_id)
            seen_vehicle_stops.add(visit_key)

            is_arriving = arrival_time.get("IsArriving") is True
            is_departed = arrival_time.get("IsDeparted") is True

            # Departure can be explicit, or inferred when the bus remains in the
            # prediction list but is no longer marked as arriving.
            if visit_key in active_visits and (is_departed or not is_arriving):
                write_completed_visit(
                    csv_writer=csv_writer,
                    csv_file=csv_file,
                    visit=active_visits[visit_key],
                    departure_time=poll_time,
                )
                departed_keys.add(visit_key)
                continue

            # Start timing on the first poll where TransLoc marks the bus as
            # arriving at this route-stop.
            if is_arriving and not is_departed and visit_key not in active_visits:
                active_visits[visit_key] = ActiveVisit(
                    route_color=route_color,
                    route_id=route_id,
                    route_stop_id=route_stop_id,
                    stop_name=configured_stop.name,
                    vehicle_number=vehicle_number,
                    arrival_time=poll_time,
                )
                print(
                    "[arrival] "
                    f"vehicle={vehicle_number} route={route_color} stop={configured_stop.name} "
                    f"route_stop_id={route_stop_id} time={format_timestamp(poll_time)}",
                    flush=True,
                )

    for visit_key in departed_keys:
        active_visits.pop(visit_key, None)

    # If an active bus vanishes from a successful poll, treat that as departure.
    # This is not done after failed polls, because failed data is not evidence.
    disappeared_keys = [
        visit_key for visit_key in active_visits if visit_key not in seen_vehicle_stops
    ]
    for visit_key in disappeared_keys:
        write_completed_visit(
            csv_writer=csv_writer,
            csv_file=csv_file,
            visit=active_visits[visit_key],
            departure_time=poll_time,
        )
        active_visits.pop(visit_key, None)


def run_tracker(
    config_path: str,
    output_path: str,
    poll_seconds: int,
    request_timeout_seconds: int,
    chunk_size: int,
) -> None:
    # Main loop for day-long collection. Completed visits are flushed to CSV
    # immediately so interruption loses at most the currently active visits.
    if poll_seconds <= 0:
        raise ValueError("--poll-seconds must be greater than zero.")
    if chunk_size <= 0:
        raise ValueError("--chunk-size must be greater than zero.")

    stop_config = load_stop_config(config_path)
    route_stop_ids = list(stop_config.keys())
    ensure_csv_header(output_path)

    active_visits: dict[tuple[str, str], ActiveVisit] = {}

    with requests.Session() as session:
        with open(output_path, "a", newline="", encoding="utf-8") as csv_file:
            csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
            print(
                f"Tracking {len(route_stop_ids)} route-stops every {poll_seconds}s. "
                f"Writing completed visits to {output_path}.",
                flush=True,
            )

            while True:
                poll_time = eastern_now()
                try:
                    stop_results = fetch_arrival_times(
                        session=session,
                        route_stop_ids=route_stop_ids,
                        chunk_size=chunk_size,
                        timeout_seconds=request_timeout_seconds,
                    )
                    try:
                        # Vehicle numbers are enrichment. If unavailable, keep
                        # collecting dwell data with internal VehicleId labels.
                        vehicle_numbers = fetch_vehicle_numbers(
                            session=session,
                            timeout_seconds=request_timeout_seconds,
                        )
                    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                        logging.warning(
                            "Vehicle number lookup failed; using internal VehicleId values "
                            "for this poll: %s",
                            exc,
                        )
                        vehicle_numbers = {}
                    process_successful_poll(
                        stop_results=stop_results,
                        stop_config=stop_config,
                        active_visits=active_visits,
                        vehicle_numbers=vehicle_numbers,
                        csv_writer=csv_writer,
                        csv_file=csv_file,
                        poll_time=poll_time,
                    )
                except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                    # Preserve active visits across request/JSON failures.
                    logging.warning("Poll failed; will retry on next interval: %s", exc)
                except KeyboardInterrupt:
                    print("Stopping all-stop dwell tracker.", flush=True)
                    return

                time.sleep(poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track observed dwell time for every configured Georgia Tech route-stop."
    )
    parser.add_argument(
        "--config",
        default="all_stops.json",
        help="Path to the all-stops config JSON.",
    )
    parser.add_argument(
        "--output",
        default="all_stop_dwell.csv",
        help="CSV path for completed all-stop dwell visits.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=5,
        help="Seconds to wait between successful or failed polls.",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=10,
        help="HTTP request timeout for each request.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=50,
        help="Number of RouteStopID values per GetStopArrivalTimes request.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()

    try:
        run_tracker(
            config_path=args.config,
            output_path=args.output,
            poll_seconds=args.poll_seconds,
            request_timeout_seconds=args.request_timeout_seconds,
            chunk_size=args.chunk_size,
        )
    except Exception as exc:
        logging.error("%s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
