import csv
import json
import math
from collections import defaultdict
from datetime import datetime

# Verified live (two separate captures, ~10 min combined): the API's own
# IsArriving flag fires 20-45+ seconds before a vehicle physically arrives,
# including while stopped at a red light/traffic 70-200m from the actual
# stop with IsArriving already True - not a reliable "physically here"
# signal. IsDeparted fired zero times across both captures - dead on this
# deployment. Arrival/departure here use GPS distance to the stop's real
# coordinates instead, cross-checked with speed.
RADIUS_METERS = 25
SPEED_THRESHOLD = 1.0


def load_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def load_stop_coords():
    with open("reference/per_route.json") as f:
        per_route = json.load(f)
    coords = {}
    for route_data in per_route.values():
        for stop in route_data["stops"]:
            # RouteStopID is an int here (from JSON) but a str once round-tripped
            # through vehicle_estimates.csv - key by str to match on lookup.
            coords[str(stop["RouteStopID"])] = (stop["Latitude"], stop["Longitude"])
    return coords


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def parse(ts):
    return datetime.fromisoformat(ts)


def at_stop(position, target_stop_id, stop_coords):
    coords = stop_coords.get(target_stop_id)
    if coords is None or position.get("Latitude") is None:
        return False
    dist = haversine_m(float(position["Latitude"]), float(position["Longitude"]), coords[0], coords[1])
    speed = float(position.get("GroundSpeed") or 0)
    return dist < RADIUS_METERS and speed < SPEED_THRESHOLD


def detect_visits(positions, vehicle_estimates, stop_coords):
    pos_by_poll = {(p["VehicleID"], p["fetched_at"]): p for p in positions}
    route_by_poll = {(p["VehicleID"], p["fetched_at"]): p["RouteID"] for p in positions}

    by_vehicle = defaultdict(list)
    for row in vehicle_estimates:
        by_vehicle[row["VehicleID"]].append(row)

    visits = []
    for vehicle_id, rows in by_vehicle.items():
        rows.sort(key=lambda r: r["fetched_at"])

        segment = []
        segment_stop = None

        def close_segment():
            if not segment:
                return
            stopped = [
                r for r in segment
                if at_stop(pos_by_poll.get((vehicle_id, r["fetched_at"]), {}), segment_stop, stop_coords)
            ]
            if not stopped:
                return  # target changed without ever registering as physically at the stop
            visits.append({
                "VehicleID": vehicle_id,
                "RouteID": route_by_poll.get((vehicle_id, segment[0]["fetched_at"])),
                "RouteStopID": segment_stop,
                "arrival_ts": stopped[0]["fetched_at"],
                "departure_ts": stopped[-1]["fetched_at"],
            })

        for row in rows:
            if row["RouteStopID"] != segment_stop:
                close_segment()
                segment = []
                segment_stop = row["RouteStopID"]
            segment.append(row)
        close_segment()

    return visits


def add_dwell_and_travel(visits):
    by_vehicle = defaultdict(list)
    for v in visits:
        by_vehicle[v["VehicleID"]].append(v)

    for vvisits in by_vehicle.values():
        vvisits.sort(key=lambda v: v["arrival_ts"])
        for i, v in enumerate(vvisits):
            v["dwell_seconds"] = (parse(v["departure_ts"]) - parse(v["arrival_ts"])).total_seconds()
            if i + 1 < len(vvisits):
                v["travel_to_next_seconds"] = (parse(vvisits[i + 1]["arrival_ts"]) - parse(v["departure_ts"])).total_seconds()
            else:
                v["travel_to_next_seconds"] = None
    return visits


if __name__ == "__main__":
    positions = load_csv("data/positions.csv")
    vehicle_estimates = load_csv("data/vehicle_estimates.csv")
    stop_coords = load_stop_coords()

    visits = detect_visits(positions, vehicle_estimates, stop_coords)
    visits = add_dwell_and_travel(visits)
    visits.sort(key=lambda v: (v["VehicleID"], v["arrival_ts"]))

    fieldnames = ["VehicleID", "RouteID", "RouteStopID", "arrival_ts", "departure_ts", "dwell_seconds", "travel_to_next_seconds"]
    with open("data/visits.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(visits)

    print(f"{len(visits)} stop visits written to data/visits.csv")
