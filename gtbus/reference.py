"""Fetch static route/stop reference data into reference/ (run once)."""
import json

from gtbus.api import get
from gtbus.paths import REFERENCE_DIR


def save(name, obj):
    with open(REFERENCE_DIR / f"{name}.json", "w") as f:
        json.dump(obj, f, indent=2)


def main():
    REFERENCE_DIR.mkdir(exist_ok=True)
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


if __name__ == "__main__":
    main()
