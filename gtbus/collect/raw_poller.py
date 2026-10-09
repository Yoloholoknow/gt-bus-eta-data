import json
import time
from datetime import datetime, timezone

from gtbus.api import get
from gtbus.paths import DATA_DIR

INTERVAL_SECONDS = 5


def poll_once():
    now = datetime.now(timezone.utc)

    vehicles = get("GetMapVehiclePoints")
    vehicle_ids = ",".join(str(v["VehicleID"]) for v in vehicles)
    vehicle_estimates = get("GetVehicleRouteStopEstimates", vehicleIdStrings=vehicle_ids) if vehicle_ids else []

    row = {
        "fetched_at": now.isoformat(),
        "vehicles": vehicles,
        "estimates": get("GetMapStopEstimates"),
        "capacities": get("GetVehicleCapacities"),
        "stop_arrivals": get("GetStopArrivalTimes"),
        "vehicle_estimates": vehicle_estimates,
        "twitter": get("GetTwitterJSON"),
    }

    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / f"{now.strftime('%Y-%m-%d')}.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"{now.isoformat()} saved {len(vehicles)} vehicles")


if __name__ == "__main__":
    while True:
        poll_once()
        time.sleep(INTERVAL_SECONDS)
