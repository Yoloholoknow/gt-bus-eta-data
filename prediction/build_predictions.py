"""Build frozen arrival-to-arrival means; no historical evaluation split."""

import argparse
import csv
import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ZoneInfo("America/New_York")
BUCKETS = (0, 30, 60, 90, 120, 150, 180, 210, 240)
COLUMNS = [
    "route_id", "origin_route_stop_id", "destination_route_stop_id",
    "origin_order", "destination_order", "horizon_stops", "route_stop_count",
    "bucket_minutes", "bucket_start_minute", "mean_elapsed_seconds",
    "sample_count", "service_date_count", "training_data_end",
    "route_definition_hash", "max_gap_seconds",
]


def parse_time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must include an explicit UTC offset")
    return result.astimezone(timezone.utc)


def load_routes(path):
    routes = {}
    for raw in json.loads(Path(path).read_text()):
        stops = sorted(raw.get("Stops", []), key=lambda item: int(item["Order"]))
        if not stops:
            continue
        normalized = [
            {"id": str(s["RouteStopID"]), "order": int(s["Order"]),
             "name": str(s.get("Description") or s.get("Name") or s["RouteStopID"]),
             "lat": float(s["Latitude"]), "lon": float(s["Longitude"])}
            for s in stops
        ]
        # The current historical schema cannot identify repeated occurrences of
        # the same route-stop ID. Refuse to silently assign the wrong occurrence.
        if len({s["id"] for s in normalized}) != len(normalized):
            raise ValueError(f"Route {raw['RouteID']} repeats a RouteStopID; explicit occurrence labels are required")
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        routes[str(raw["RouteID"])] = {
            "name": str(raw.get("Description", raw["RouteID"])),
            "stops": normalized, "hash": digest,
        }
    return routes


def atomic_csv(path, columns, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build(visits_path, routes_path, output_path, max_gap_seconds):
    if not math.isfinite(max_gap_seconds) or max_gap_seconds <= 0:
        raise ValueError("max_gap_seconds must be positive and finite")
    routes = load_routes(routes_path)
    by_vehicle = defaultdict(list)
    diagnostics = Counter()
    seen = set()
    latest = None
    with Path(visits_path).open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        required = {"route_id", "route_stop_id", "vehicle_number", "at_stop_time"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Visits CSV must contain {sorted(required)}")
        for row in reader:
            diagnostics["input_rows"] += 1
            vid = row["vehicle_number"].strip()
            try:
                at = parse_time(row["at_stop_time"])
                if not vid:
                    raise ValueError("Missing vehicle")
            except (ValueError, TypeError):
                diagnostics["invalid_timestamp_or_vehicle"] += 1
                # A row that cannot be placed in time breaks continuity for
                # this vehicle in the input stream.
                if vid:
                    by_vehicle[vid].append(None)
                continue
            key = (vid, row["route_id"], row["route_stop_id"], at)
            if key in seen:
                diagnostics["duplicates"] += 1
                continue
            seen.add(key)
            rid, sid = row["route_id"], row["route_stop_id"]
            route = routes.get(rid)
            index = next((i for i, s in enumerate(route["stops"]) if s["id"] == sid), -1) if route else -1
            # Unknown route/stop rows remain as continuity barriers.
            by_vehicle[vid].append((at, rid, index))
            if index >= 0:
                latest = max(latest, at) if latest else at
                diagnostics["valid_visits"] += 1

    # Values are [sum, count, set of origin local dates]. No point in keeping
    # every elapsed duration in memory for an arithmetic mean.
    stats = {}

    def process_segment(segment):
        segment.sort(key=lambda visit: visit[0])
        for start, (origin_at, rid, origin_index) in enumerate(segment):
            if origin_index < 0:
                continue
            stops = routes[rid]["stops"]
            n = len(stops)
            local = origin_at.astimezone(LOCAL)
            minute = local.hour * 60 + local.minute
            previous_at = origin_at
            for horizon in range(1, n + 1):
                if start + horizon >= len(segment):
                    break
                at, dest_route, dest_index = segment[start + horizon]
                gap = (at - previous_at).total_seconds()
                if (dest_route != rid or dest_index != (origin_index + horizon) % n
                        or not 0 < gap <= max_gap_seconds):
                    diagnostics["broken_sequences"] += 1
                    break
                elapsed = (at - origin_at).total_seconds()
                for width in BUCKETS:
                    bucket = minute // width * width if width else ""
                    key = (rid, origin_index, horizon, width, bucket)
                    item = stats.setdefault(key, [0.0, 0, set()])
                    item[0] += elapsed
                    item[1] += 1
                    item[2].add(local.date().isoformat())
                diagnostics["journey_examples"] += 1
                previous_at = at

    for visits in by_vehicle.values():
        segment = []
        for visit in visits + [None]:
            if visit is None:
                process_segment(segment)
                segment = []
            else:
                segment.append(visit)
    if not stats:
        raise ValueError("No complete journeys found; averages CSV was not changed")

    def output_rows():
        for (rid, origin, horizon, width, bucket), (total, count, dates) in sorted(stats.items()):
            route = routes[rid]
            stops = route["stops"]
            destination = (origin + horizon) % len(stops)
            yield dict(zip(COLUMNS, [
                rid, stops[origin]["id"], stops[destination]["id"], origin, destination,
                horizon, len(stops), width, bucket, format(total / count, ".9f"),
                count, len(dates), latest.isoformat(), route["hash"], max_gap_seconds,
            ]))

    atomic_csv(output_path, COLUMNS, output_rows())
    diagnostics["average_rows"] = len(stats)
    return dict(diagnostics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--visits", type=Path, default=ROOT / "data/stops_timings/combined_stop_visits.csv")
    parser.add_argument("--routes", type=Path, default=ROOT / "reference/routes.json")
    parser.add_argument("--output", type=Path, default=ROOT / "prediction/averages.csv")
    parser.add_argument("--max-gap-seconds", type=float, default=3600,
                        help="Maximum gap between successive stop arrivals; breaks incomplete journeys")
    args = parser.parse_args()
    print(json.dumps(build(args.visits, args.routes, args.output, args.max_gap_seconds), indent=2))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
