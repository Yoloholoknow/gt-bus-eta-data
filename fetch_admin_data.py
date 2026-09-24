"""
Tested live against bus.gatech.edu: GetBadgeScanData returns 200 with an
empty list (no badge-scan data configured on this deployment).
GetRidershipData times out entirely - doesn't respond, effectively
broken here. Neither is needed for ETA work anyway (fare/ridership
counts, not location or timing) - GetVehicleCapacities already covers
crowding. Left here for reference only, not part of the pipeline.
"""
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://bus.gatech.edu/Services/JSONPRelay.svc"

end = datetime.now(timezone.utc)
start = end - timedelta(days=1)
params = {"StartDate": start.isoformat(), "EndDate": end.isoformat()}

for method in ["GetBadgeScanData", "GetRidershipData"]:
    resp = requests.get(f"{BASE}/{method}", params=params, timeout=15)
    print(method, resp.status_code, resp.text[:200])
