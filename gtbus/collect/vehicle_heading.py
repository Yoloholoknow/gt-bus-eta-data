"""Collect vehicle positions into daily CSV files for the local dashboard.

The raw JSONL poller remains the source of truth for the broader project. This
small collector is intentionally focused on the vehicle-position fields needed
for heading, speed, and map visualizations.
"""

import argparse
import csv
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from gtbus.api import BASE_URL
from gtbus.paths import DATA_DIR


DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_OUTPUT_DIR = DATA_DIR / "vehicle_heading"
TICK_STATE_FILENAME = ".next_tick"

POINT_COLUMNS = [
    "tick",
    "fetched_at",
    "source_timestamp",
    "seconds",
    "vehicle_id",
    "vehicle_name",
    "route_id",
    "latitude",
    "longitude",
    "ground_speed",
    "heading",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        if needs_header:
            writer.writeheader()
        writer.writerows(rows)
        output.flush()


def daily_path(output_dir: Path, prefix: str, now: datetime) -> Path:
    return output_dir / f"{prefix}_{now.strftime('%Y-%m-%d')}.csv"


def recover_next_tick(output_dir: Path) -> int:
    """Recover the next tick from durable state or existing CSV files."""
    state_path = output_dir / TICK_STATE_FILENAME
    try:
        saved_next_tick = int(state_path.read_text(encoding="utf-8").strip())
        if saved_next_tick > 0:
            return saved_next_tick
    except (OSError, ValueError):
        pass

    largest_tick = 0
    for path in output_dir.glob("*.csv"):
        try:
            with path.open(newline="", encoding="utf-8") as source:
                for row in csv.DictReader(source):
                    try:
                        largest_tick = max(largest_tick, int(row.get("tick", 0)))
                    except (TypeError, ValueError):
                        continue
        except (OSError, csv.Error):
            continue
    return largest_tick + 1


def reserve_next_tick(output_dir: Path, next_tick: int) -> None:
    """Persist the next unused tick before starting its API request."""
    state_path = output_dir / TICK_STATE_FILENAME
    temporary_path = output_dir / f"{TICK_STATE_FILENAME}.tmp"
    temporary_path.write_text(str(next_tick), encoding="utf-8")
    os.replace(temporary_path, state_path)


def collect_once(
    session: requests.Session,
    output_dir: Path,
    tick: int,
    timeout_seconds: float,
) -> None:
    started = utc_now()
    started_monotonic = time.monotonic()
    response: requests.Response | None = None
    vehicles: list[dict[str, Any]] = []
    error = ""
    ok = False

    try:
        response = session.get(
            f"{BASE_URL}/GetMapVehiclePoints",
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("GetMapVehiclePoints response was not a list")
        vehicles = [item for item in payload if isinstance(item, dict)]
        ok = True
    except (requests.RequestException, ValueError, TypeError) as exc:
        error = f"{type(exc).__name__}: {exc}"

    duration_ms = round((time.monotonic() - started_monotonic) * 1000, 1)

    if ok:
        point_rows = [
            {
                "tick": tick,
                "fetched_at": timestamp(started),
                "source_timestamp": vehicle.get("TimeStamp", ""),
                "seconds": vehicle.get("Seconds", ""),
                "vehicle_id": vehicle.get("VehicleID", ""),
                "vehicle_name": vehicle.get("Name", ""),
                "route_id": vehicle.get("RouteID", ""),
                "latitude": vehicle.get("Latitude", ""),
                "longitude": vehicle.get("Longitude", ""),
                "ground_speed": vehicle.get("GroundSpeed", ""),
                "heading": vehicle.get("Heading", ""),
            }
            for vehicle in vehicles
        ]
        append_csv(
            daily_path(output_dir, "vehicle_points", started),
            POINT_COLUMNS,
            point_rows,
        )

    if ok:
        logging.info("tick=%s saved %s vehicles in %sms", tick, len(vehicles), duration_ms)
    else:
        logging.warning("tick=%s collection failed in %sms: %s", tick, duration_ms, error)


def run(output_dir: Path, interval_seconds: float, timeout_seconds: float) -> None:
    if interval_seconds <= 0:
        raise ValueError("interval must be greater than zero")
    if timeout_seconds <= 0:
        raise ValueError("timeout must be greater than zero")

    output_dir.mkdir(parents=True, exist_ok=True)
    next_poll = time.monotonic()
    tick = recover_next_tick(output_dir)

    with requests.Session() as session:
        while True:
            # Reserve this value before the request. If the process crashes,
            # the next run skips it instead of reusing the same tick.
            reserve_next_tick(output_dir, tick + 1)
            collect_once(session, output_dir, tick, timeout_seconds)
            tick += 1

            # Schedule against the original cadence instead of sleeping for the
            # interval after the request completes. This keeps poll starts close
            # to every five seconds when the API responds quickly.
            next_poll += interval_seconds
            delay = next_poll - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_poll = time.monotonic()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()
    try:
        run(args.output_dir, args.interval, args.timeout)
    except KeyboardInterrupt:
        logging.info("collector stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
