import csv
import glob
import json

FACT_FIELDS = {
    "vehicles": ["VehicleID", "RouteID", "Latitude", "Longitude", "GroundSpeed", "Heading", "IsOnRoute", "IsDelayed", "Seconds"],
    "capacities": ["VehicleID", "Capacity", "CurrentOccupation", "Percentage"],
}


def load_polls():
    polls = []
    for path in sorted(glob.glob("data/*.jsonl")):
        with open(path) as f:
            polls.extend(json.loads(line) for line in f)
    return polls


def flatten_positions(polls):
    for r in polls:
        for v in r["vehicles"]:
            yield {**{k: v.get(k) for k in FACT_FIELDS["vehicles"]}, "fetched_at": r["fetched_at"]}


def flatten_capacities(polls):
    for r in polls:
        for c in r["capacities"]:
            yield {**{k: c.get(k) for k in FACT_FIELDS["capacities"]}, "fetched_at": r["fetched_at"]}


def flatten_vehicle_estimates(polls):
    # One row per vehicle's currently-targeted stop each poll (GetVehicleRouteStopEstimates).
    # This is the key stream arrivals.py uses to detect arrival/departure.
    for r in polls:
        for v in r["vehicle_estimates"]:
            vehicle_id = v.get("VehicleID")
            for e in v.get("Estimates", []):
                yield {
                    "VehicleID": vehicle_id,
                    "RouteStopID": e.get("RouteStopID"),
                    "Description": e.get("Description"),
                    "Seconds": e.get("Seconds"),
                    "OnRoute": e.get("OnRoute"),
                    "fetched_at": r["fetched_at"],
                }


def flatten_stop_estimates(polls):
    # GetMapStopEstimates: per-route, per-stop, per-vehicle ETA. Not used by
    # arrivals.py (vehicle_estimates is simpler for that), kept for anyone
    # who wants the stop-centric view instead of the vehicle-centric one.
    for r in polls:
        for route in r["estimates"]:
            route_id = route.get("RouteID")
            for stop in route.get("RouteStops", []):
                stop_id = stop.get("RouteStopID")
                for e in stop.get("Estimates", []):
                    yield {
                        "RouteID": route_id,
                        "RouteStopID": stop_id,
                        "VehicleID": e.get("VehicleID"),
                        "SecondsToStop": e.get("SecondsToStop"),
                        "OnRoute": e.get("OnRoute"),
                        "fetched_at": r["fetched_at"],
                    }


def flatten_stop_arrivals(polls):
    for r in polls:
        for stop in r["stop_arrivals"]:
            route_id = stop.get("RouteId", stop.get("RouteID"))
            stop_id = stop.get("RouteStopId", stop.get("RouteStopID"))
            for t in stop.get("Times", []):
                yield {
                    "RouteID": route_id,
                    "RouteStopID": stop_id,
                    "VehicleID": t.get("VehicleId", t.get("VehicleID")),
                    "Seconds": t.get("Seconds"),
                    "OnTimeStatus": t.get("OnTimeStatus"),
                    "fetched_at": r["fetched_at"],
                }


def write_csv(rows, path):
    rows = list(rows)
    if not rows:
        print(f"{path}: no rows")
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"{path}: {len(rows)} rows")


if __name__ == "__main__":
    polls = load_polls()
    print(f"{len(polls)} polls loaded")
    write_csv(flatten_positions(polls), "data/positions.csv")
    write_csv(flatten_capacities(polls), "data/capacities.csv")
    write_csv(flatten_vehicle_estimates(polls), "data/vehicle_estimates.csv")
    write_csv(flatten_stop_estimates(polls), "data/stop_estimates.csv")
    write_csv(flatten_stop_arrivals(polls), "data/stop_arrivals.csv")
