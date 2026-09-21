import json
import time
from datetime import datetime, timezone

import requests

BASE = "https://bus.gatech.edu/Services/JSONPRelay.svc"
INTERVAL_SECONDS = 5


def get(method, **params):
    resp = requests.get(f"{BASE}/{method}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


while True:
    now = datetime.now(timezone.utc)

    vehicles = get("GetMapVehiclePoints")
    estimates = get("GetMapStopEstimates")
    capacities = get("GetVehicleCapacities")
    stop_arrivals = get("GetStopArrivalTimes")
    twitter = get("GetTwitterJSON")

    vehicle_ids = ",".join(str(v["VehicleID"]) for v in vehicles)
    vehicle_estimates = get("GetVehicleRouteStopEstimates", vehicleIdStrings=vehicle_ids) if vehicle_ids else []

    row = {
        "fetched_at": now.isoformat(),
        "vehicles": vehicles,
        "estimates": estimates,
        "capacities": capacities,
        "stop_arrivals": stop_arrivals,
        "vehicle_estimates": vehicle_estimates,
        "twitter": twitter,
    }

    filename = f"data/{now.strftime('%Y-%m-%d')}.jsonl"
    with open(filename, "a") as f:
        f.write(json.dumps(row) + "\n")

    print(f"{now.isoformat()} saved {len(vehicles)} vehicles")
    time.sleep(INTERVAL_SECONDS)
