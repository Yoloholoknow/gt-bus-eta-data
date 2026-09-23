import csv
from collections import defaultdict
from datetime import datetime

# A vehicle "counts" as stopped below this speed. Known limitation: a bus
# stuck in traffic while still approaching its target stop can register as
# stopped here too, inflating dwell time for that visit. If that turns out
# to matter, the fix is cross-checking GPS distance to the stop's actual
# lat/lon (in reference/per_route.json) instead of relying on speed alone.
SPEED_THRESHOLD = 1.0


def load_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def parse(ts):
    return datetime.fromisoformat(ts)


def detect_visits(positions, vehicle_estimates):
    speed = {(p["VehicleID"], p["fetched_at"]): float(p["GroundSpeed"] or 0) for p in positions}
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
            stopped = [r for r in segment if speed.get((vehicle_id, r["fetched_at"]), 0) < SPEED_THRESHOLD]
            if not stopped:
                return  # target changed without ever registering as stopped - drove through without a hold
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

    visits = detect_visits(positions, vehicle_estimates)
    visits = add_dwell_and_travel(visits)
    visits.sort(key=lambda v: (v["VehicleID"], v["arrival_ts"]))

    fieldnames = ["VehicleID", "RouteID", "RouteStopID", "arrival_ts", "departure_ts", "dwell_seconds", "travel_to_next_seconds"]
    with open("data/visits.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(visits)

    print(f"{len(visits)} stop visits written to data/visits.csv")
