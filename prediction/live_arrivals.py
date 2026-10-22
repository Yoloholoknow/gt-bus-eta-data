"""Local, continuously updated stop arrivals combining TransLoc and frozen means.

No server, training, or data writes. Run with --help for examples/options.
"""

import argparse
import csv
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    from .build_predictions import BUCKETS, LOCAL, ROOT, load_routes, parse_time
    from .eval_loop import BASE_URL, Model, source_timestamp, stamp
except ImportError:
    from build_predictions import BUCKETS, LOCAL, ROOT, load_routes, parse_time
    from eval_loop import BASE_URL, Model, source_timestamp, stamp


OFFSET_COLUMNS = (
    "4-6 AM", "6-8 AM", "8-10 AM", "10 AM-12 PM", "12-2 PM",
    "2-4 PM", "4-6 PM", "6-8 PM", "8-10 PM", "10 PM-12 AM",
)


def load_offsets(path):
    """Read editable seconds per route and local two-hour block."""
    result = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        columns = ["route_color", *OFFSET_COLUMNS]
        if len(reader.fieldnames or []) != len(columns) or set(reader.fieldnames or []) != set(columns):
            raise ValueError(f"Offset CSV must have columns: {', '.join(columns)}")
        for row in reader:
            name = (row["route_color"] or "").strip().casefold()
            if not name or name in result or None in row:
                raise ValueError(f"Invalid or duplicate route in offset CSV, line {reader.line_num}")
            values = []
            for column in OFFSET_COLUMNS:
                try:
                    value = float(row[column])
                    if not math.isfinite(value):
                        raise ValueError("Nonfinite offset")
                except (TypeError, ValueError):
                    raise ValueError(f"Invalid offset for {row['route_color']}, {column}: use numeric seconds") from None
                values.append(value)
            result[name] = values
    return result


def route_offset(offsets, route_name, now):
    hour = now.astimezone(LOCAL).hour
    if hour < 4:
        return 0, None
    index = (hour - 4) // 2
    values = offsets.get(route_name.strip().casefold())
    return (values[index] if values is not None else 0), OFFSET_COLUMNS[index]


def select_route(routes, query):
    matches = [rid for rid, route in routes.items()
               if query.casefold() in (rid.casefold(), route["name"].casefold())]
    if len(matches) != 1:
        raise ValueError("Choose a route name/color or ID: " + ", ".join(
            f"{r['name']} ({rid})" for rid, r in routes.items()))
    return matches[0]


def select_stop(stops, query):
    normalized = " ".join(query.casefold().split())
    exact = [i for i, stop in enumerate(stops)
             if normalized in (stop["id"], " ".join(stop["name"].casefold().split()))]
    matches = exact or [i for i, stop in enumerate(stops)
                        if normalized in " ".join(stop["name"].casefold().split())]
    if len(matches) != 1:
        choices = matches or range(len(stops))
        raise ValueError("Stop is ambiguous or unknown. Use a RouteStopID or a more specific name:\n" +
                         "\n".join(f"  {stops[i]['id']}: {stops[i]['name']}" for i in choices))
    return matches[0]


def fetch(base, method, params, timeout):
    url = base.rstrip("/") + "/" + method + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "gt-bus-local-arrivals/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    if not isinstance(data, list):
        raise ValueError(f"{method} returned something other than a list")
    return data, datetime.now(timezone.utc)


def parse_entry(entry, received):
    """Normalize one estimate, retaining arrival flags and occurrence identity."""
    if not isinstance(entry, dict) or entry.get("IsDeparted") or entry.get("OnRoute") is False:
        return None
    try:
        if entry.get("EstimateTime"):
            arrival = source_timestamp(entry["EstimateTime"])
        else:
            seconds = float(entry["Seconds"])
            if seconds < 0:
                return None
            arrival = received.timestamp() + seconds
        if not math.isfinite(arrival):
            return None
        arriving = entry.get("IsArriving") is True
        if arrival < received.timestamp():
            if not arriving:
                return None
            arrival = received.timestamp()
        return {"arrival": arrival, "arriving": arriving,
                "stop_id": str(entry.get("RouteStopID", entry.get("RouteStopId", "")))}
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def parse_estimates(data, rid, received, target_id):
    """Nearest occurrences per vehicle at the selected route-stop, not schedules."""
    estimates = {}
    for stop in data:
        if not isinstance(stop, dict):
            continue
        if (str(stop.get("RouteId", stop.get("RouteID", ""))) != rid
                or str(stop.get("RouteStopId", stop.get("RouteStopID", ""))) != target_id):
            continue
        for entry in stop.get("Times", []) or []:
            normalized = parse_entry(entry, received)
            if normalized is None:
                continue
            vid = entry.get("VehicleId", entry.get("VehicleID"))
            if vid is not None:
                estimates.setdefault(str(vid), []).append(normalized)
    for values in estimates.values():
        values.sort(key=lambda value: value["arrival"])
    return estimates


def parse_vehicle_estimates(data, rid, stops, requested, received):
    """Normalize upcoming stops without guessing from GPS or response order.

    A vehicle reporting another route's stops is unsafe to anchor: it may have
    changed routes between the selected-stop and vehicle requests.
    """
    result = {}
    known_stops = {stop["id"] for stop in stops}
    invalid = set()
    for vehicle in data:
        if not isinstance(vehicle, dict):
            continue
        vid = str(vehicle.get("VehicleID", vehicle.get("VehicleId", "")))
        if vid not in requested:
            continue
        route_id = str(vehicle.get("RouteID", vehicle.get("RouteId", rid)))
        if route_id != rid:
            invalid.add(vid)
            continue
        for entry in vehicle.get("Estimates", []) or []:
            normalized = parse_entry(entry, received)
            if normalized is None:
                continue
            entry_vid = str(entry.get("VehicleId", entry.get("VehicleID", vid)))
            entry_route = str(entry.get("RouteID", entry.get("RouteId", rid)))
            if entry_vid != vid or entry_route != rid or normalized["stop_id"] not in known_stops:
                invalid.add(vid)
                continue
            result.setdefault(vid, []).append(normalized)
    for vid in invalid:
        result.pop(vid, None)
    for values in result.values():
        values.sort(key=lambda value: value["arrival"])
    return result


def fresh(received, now, max_age):
    return received is not None and 0 <= (now - received).total_seconds() <= max_age


def predict_arrivals(model, rid, destination, estimates, estimates_at,
                     upcoming, upcoming_at, now, width=0, max_age=30, names=None,
                     offset_seconds=0):
    """Pure snapshot calculation: no previous polls, GPS history, or dwell state."""
    if not fresh(estimates_at, now, max_age):
        return []
    if not fresh(upcoming_at, now, max_age):
        upcoming = {}
    names = names or {}
    stops = model.routes[rid]["stops"]
    indices = {stop["id"]: i for i, stop in enumerate(stops)}
    rows = []
    for vid, occurrences in estimates.items():
        vendor = occurrences[0]
        vendor_at = vendor["arrival"]
        row = {"vehicle_id": vid, "bus": names.get(vid, vid),
               "predicted_arrival": vendor_at, "source": "transloc-fallback",
               "reason": "No fresh, usable future-stop estimates for this vehicle",
               "transloc_arrival": vendor_at, "anchor_stop": None, "anchor_stop_id": None,
               "anchor_seconds": None, "model_horizon": None,
               "sample_count": None, "bucket_fallback": False, "model_bucket_minutes": None}
        if vendor["arriving"]:
            row.update(predicted_arrival=now.timestamp(), source="transloc-arriving",
                       reason="TransLoc marks the selected stop as arriving")
        else:
            # Skip a current/arriving stop: its elapsed dwell is unknown. The
            # future-stop estimate accounts for remaining dwell and travel.
            candidates = [entry for entry in upcoming.get(vid, [])
                          if not entry["arriving"] and entry["arrival"] > now.timestamp()]
            if candidates:
                first = candidates[0]
                ties = {entry["stop_id"] for entry in candidates
                        if entry["arrival"] == first["arrival"]}
                anchor = indices[first["stop_id"]]
                next_at = first["arrival"]
                if len(ties) != 1:
                    row["reason"] = "Earliest future stop is ambiguous; using TransLoc"
                elif vendor_at < next_at and anchor != destination:
                    row["reason"] = "Selected arrival precedes the future anchor; using TransLoc"
                elif anchor == destination:
                    # Keep the selected-stop endpoint's nearest occurrence;
                    # do not substitute a later-circuit vehicle estimate.
                    row.update(source="transloc-next-stop", model_horizon=0,
                               reason="Selected stop is the next future stop; no onward model needed",
                               anchor_stop=stops[anchor]["name"], anchor_stop_id=stops[anchor]["id"],
                               anchor_seconds=max(0, round(next_at - now.timestamp())))
                else:
                    horizon = (destination - anchor) % len(stops)
                    origin_at = datetime.fromtimestamp(next_at, timezone.utc)
                    item, fallback, _ = model.lookup(rid, anchor, horizon, width, origin_at)
                    row.update(anchor_stop=stops[anchor]["name"], anchor_stop_id=stops[anchor]["id"],
                               anchor_seconds=max(0, round(next_at - now.timestamp())))
                    if item:
                        row.update(predicted_arrival=next_at + item[0], source="hybrid",
                                   reason="TransLoc to next future stop + direct model onward",
                                   model_horizon=horizon, sample_count=item[1], bucket_fallback=fallback,
                                   model_bucket_minutes=0 if fallback else width)
                    else:
                        row["reason"] = "No historical mean for the onward journey"
        original = max(now.timestamp(), row["predicted_arrival"])
        # An arriving bus is already here; an offset must not turn that into
        # a future arrival. All other final ETAs receive the correction once.
        adjusted = (original if row["source"] == "transloc-arriving"
                    else max(now.timestamp(), original + offset_seconds))
        row.update(predicted_arrival=adjusted, offset_seconds=offset_seconds,
                   applied_offset_seconds=adjusted - original,
                   unadjusted_predicted_arrival=stamp(datetime.fromtimestamp(original, timezone.utc)))
        rows.append(row)
    rows.sort(key=lambda row: (row["predicted_arrival"], row["vehicle_id"]))
    for row in rows:
        for prefix in ("predicted", "transloc"):
            arrival = row[prefix + "_arrival"]
            row[prefix + "_seconds"] = max(0, round(arrival - now.timestamp()))
            row[prefix + "_arrival"] = stamp(datetime.fromtimestamp(arrival, timezone.utc))
    return rows


def collect_snapshot(pool, model, rid, destination, base_url, timeout, max_age, width,
                     vehicle_names=False, offsets=None):
    """Two-stage fetch; GPS is optional display metadata, never prediction state."""
    route = model.routes[rid]
    stop = route["stops"][destination]
    stop_future = pool.submit(fetch, base_url, "GetStopArrivalTimes", {
        "routeStopIDs": stop["id"], "timesPerStop": 30,
    }, timeout)
    names_future = (pool.submit(fetch, base_url, "GetMapVehiclePoints", {"routeID": rid}, timeout)
                    if vehicle_names else None)
    warnings = []
    estimates, estimates_at, upcoming, upcoming_at, names = {}, None, {}, None, {}
    try:
        data, estimates_at = stop_future.result()
        estimates = parse_estimates(data, rid, estimates_at, stop["id"])
    except (OSError, ValueError) as exc:
        warnings.append(f"Selected-stop request failed: {exc}")
    if estimates:
        try:
            data, upcoming_at = fetch(base_url, "GetVehicleRouteStopEstimates", {
                "vehicleIdStrings": ",".join(sorted(estimates)),
                "quantity": len(route["stops"]) + 1,
            }, timeout)
            upcoming = parse_vehicle_estimates(data, rid, route["stops"], set(estimates), upcoming_at)
        except (OSError, ValueError) as exc:
            warnings.append(f"Upcoming-stop request failed: {exc}")
    # Do not hold up a prediction for GPS. Slow requests are cancelled if not
    # yet started; running label requests finish within their request timeout.
    if names_future is not None and names_future.done():
        try:
            points, _ = names_future.result()
            names = {str(p["VehicleID"]): str(p.get("Name", p["VehicleID"])) for p in points
                     if isinstance(p, dict) and p.get("VehicleID") is not None and str(p.get("RouteID")) == rid}
        except (OSError, ValueError):
            pass  # Vehicle IDs remain usable labels when GPS is unavailable.
    elif names_future is not None:
        names_future.cancel()
    now = datetime.now(timezone.utc)
    offset_seconds, offset_block = route_offset(offsets or {}, route["name"], now)
    if estimates_at is not None and not fresh(estimates_at, now, max_age):
        warnings.append("Selected-stop response became stale during this fetch; no arrivals displayed")
    return {"updated_at": stamp(now), "route": route["name"], "route_id": rid,
            "stop_name": stop["name"], "route_stop_id": stop["id"],
            "bucket_minutes": width, "warnings": warnings,
            "offset_block": offset_block, "route_offset_seconds": offset_seconds,
            "buses": predict_arrivals(model, rid, destination, estimates, estimates_at,
                                      upcoming, upcoming_at, now, width, max_age, names, offset_seconds)}


def duration(seconds):
    return "--" if seconds is None else f"{seconds // 60}:{seconds % 60:02d}"


def display(snapshot, as_json):
    if as_json:
        print(json.dumps(snapshot), flush=True)
        return
    now = parse_time(snapshot["updated_at"]).astimezone(LOCAL)
    print(f"\n{now:%Y-%m-%d %H:%M:%S %Z} | {snapshot['route']} | "
          f"{snapshot['stop_name']} (RouteStopID {snapshot['route_stop_id']})")
    for warning in snapshot["warnings"]:
        print("Warning:", warning)
    print(f"{'BUS (ID)':<17} {'PREDICTED':<11} {'ARRIVAL':<12} {'TRANSLOC':<10} {'OFFSET':<9} SOURCE / MODEL ANCHOR")
    for row in snapshot["buses"]:
        clock = (parse_time(row["predicted_arrival"]).astimezone(LOCAL).strftime("%I:%M:%S %p")
                 if row["predicted_arrival"] else "--")
        label = (f"{row['bus']} ({row['vehicle_id']})" if row["bus"] != row["vehicle_id"]
                 else f"Vehicle {row['vehicle_id']}")
        anchor = (f" / {row['anchor_stop']} → target ({row['model_horizon']} stops)"
                  if row["model_horizon"] else "")
        fallback = " [unbucketed mean]" if row["bucket_fallback"] else ""
        offset = f"{row['applied_offset_seconds']:+g}s"
        print(f"{label:<17} {duration(row['predicted_seconds']):<11} {clock:<12} "
              f"{duration(row['transloc_seconds']):<10} {offset:<9} {row['source']}{anchor}{fallback}")
        if row["source"] in ("transloc-fallback", "unavailable"):
            print(" " * 19 + row["reason"])
    if not snapshot["buses"]:
        print("No live buses or vehicle-specific estimates available for this route.")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Example: .venv/bin/python prediction/live_arrivals.py --route Red --stop "West Village"'))
    parser.add_argument("--route", required=True, help="Route color/name or RouteID, case insensitive")
    parser.add_argument("--stop", help="RouteStopID, exact name, or unique part of a name")
    parser.add_argument("--list-stops", action="store_true", help="List this route's stop IDs and exit")
    parser.add_argument("--once", action="store_true", help="One complete snapshot; hybrid estimates work on the first poll")
    parser.add_argument("--json", action="store_true", help="One JSON snapshot per poll (JSON Lines)")
    parser.add_argument("--vehicle-names", action="store_true", help="Optionally fetch fleet names; predictions only need vehicle IDs")
    parser.add_argument("--bucket-minutes", type=int, choices=BUCKETS, default=0)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--max-age-seconds", type=float, default=30)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--min-dates", type=int, default=3)
    parser.add_argument("--duration-seconds", type=float, help="Stop after this many seconds")
    parser.add_argument("--routes", type=Path, default=ROOT / "reference/routes.json")
    parser.add_argument("--averages", type=Path, default=ROOT / "prediction/averages.csv")
    parser.add_argument("--offsets", type=Path, default=ROOT / "prediction/route_time_offsets.csv",
                        help="Route/time offsets in seconds; reloaded each poll")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()
    for name in ("interval_seconds", "timeout_seconds", "max_age_seconds", "min_samples", "min_dates", "duration_seconds"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"--{name.replace('_', '-')} must be positive and finite")
    try:
        routes = load_routes(args.routes)
        rid = select_route(routes, args.route)
        route = routes[rid]
        if args.list_stops:
            for stop in route["stops"]:
                print(f"{stop['id']}: {stop['name']}")
            return
        if not args.stop:
            parser.error("--stop is required unless --list-stops is used")
        destination = select_stop(route["stops"], args.stop)
        model = Model(args.averages, routes, args.min_samples, args.min_dates)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            while True:
                poll_started = time.monotonic()
                offsets = load_offsets(args.offsets)
                snapshot = collect_snapshot(pool, model, rid, destination, args.base_url,
                                            args.timeout_seconds, args.max_age_seconds, args.bucket_minutes,
                                            args.vehicle_names, offsets)
                display(snapshot, args.json)
                if args.once or (args.duration_seconds and time.monotonic() - started >= args.duration_seconds):
                    break
                delay = max(0, args.interval_seconds - (time.monotonic() - poll_started))
                if args.duration_seconds:
                    remaining = args.duration_seconds - (time.monotonic() - started)
                    if remaining <= delay:
                        break
                time.sleep(delay)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
