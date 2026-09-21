# gt-bus-eta-data

Skeleton for collecting GT bus data (`bus.gatech.edu`, no API key needed) and figuring out, as a team, how to analyze it toward better ETA predictions.

Deliberately minimal — this is a starting point for the team to design the analysis together, not a finished pipeline.

## Setup

```
pip install -r requirements.txt
```

## Reference data (run once)

```
python3 fetch_reference.py
```

Pulls the static stuff that barely changes — routes, stops, markers, route schedules, map config — into `reference/`. Run once at the start, or again if the route list changes.

## Poll (run continuously)

```
python3 poller.py
```

Every 5s, hits every live-changing endpoint: vehicle positions, stop estimates, vehicle-keyed estimates, vehicle capacities, stop arrival times, Twitter feed. Appends one JSON line per poll to `data/YYYY-MM-DD.jsonl`. Ctrl+C to stop.

`fetch_admin_data.py` covers `GetBadgeScanData`/`GetRidershipData` separately — those need a date range and are untested for access on this deployment, so they're not wired into the loop.

## Analyze

```
python3 analyze.py
```

Loads everything in `data/`, prints basic per-endpoint counts (vehicles, capacities, estimates). Everything past that is intentionally unimplemented — see the comment block in `analyze.py` for the open questions (how to derive actual arrival time, what to group error by, how the known long-dwell stops should factor in).

Related issues: gtiosclub/Georgia-Tech-App#237-#241.
