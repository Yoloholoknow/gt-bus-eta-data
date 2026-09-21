import json
import os

import requests

BASE = "https://bus.gatech.edu/Services/JSONPRelay.svc"


def get(method, **params):
    resp = requests.get(f"{BASE}/{method}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def save(name, obj):
    with open(f"reference/{name}.json", "w") as f:
        json.dump(obj, f, indent=2)


os.makedirs("reference", exist_ok=True)

routes = get("GetRoutesForMapWithScheduleWithEncodedLine")
save("routes", routes)
save("map_config", get("GetMapConfig"))
save("all_routes_catalog", get("GetRoutes"))  # includes old/inactive routes, not just the active ones above

per_route = {}
for r in routes:
    rid = r["RouteID"]
    per_route[rid] = {
        "stops": get("GetStops", routeID=rid),
        "markers": get("GetMarkers", routeID=rid),
        "schedules": get("GetRouteSchedules", routeID=rid),
        "schedule_times": get("GetRouteScheduleTimes", routeIDString=str(rid)),
    }
save("per_route", per_route)

print(f"Saved reference data for {len(routes)} active routes to reference/")
