import json
import time
from datetime import datetime, timezone

import requests

BASE = "https://bus.gatech.edu/Services/JSONPRelay.svc"
INTERVAL_SECONDS = 5

while True:
    now = datetime.now(timezone.utc)
    vehicles = requests.get(f"{BASE}/GetMapVehiclePoints").json()
    estimates = requests.get(f"{BASE}/GetMapStopEstimates").json()

    row = {
        "fetched_at": now.isoformat(),
        "vehicles": vehicles,
        "estimates": estimates,
    }

    filename = f"data/{now.strftime('%Y-%m-%d')}.jsonl"
    with open(filename, "a") as f:
        f.write(json.dumps(row) + "\n")

    print(f"{now.isoformat()} saved {len(vehicles)} vehicles")
    time.sleep(INTERVAL_SECONDS)
