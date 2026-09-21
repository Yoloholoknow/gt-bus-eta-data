"""
GetBadgeScanData / GetRidershipData need a date range and appear to be
admin-scoped (untested against GT's deployment — may 401 or return
empty). Not part of the regular poller since they're a batch pull over
a date range, not a live-state snapshot. Run manually if access is
confirmed.
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
