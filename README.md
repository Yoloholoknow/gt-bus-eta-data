# gt-bus-eta-data

Collecting GT bus data (`bus.gatech.edu`, no API key needed) and figuring out, as a team, how to analyze it toward better ETA predictions.

## Setup

```
pip install -r requirements.txt
```

## Pipeline

Four stages, run in order:

**1. Reference data (once)**
```
python3 fetch_reference.py
```
Routes, stops, markers, route schedules, map config — the stuff that barely changes. Saved to `reference/`.

**2. Poll (continuously, for the collection window)**
```
python3 poller.py
```
Every 5s, hits every live-changing endpoint: vehicle positions, stop estimates (both the per-route and per-vehicle shapes), vehicle capacities, stop arrival times, Twitter feed. Appends one JSON line per poll to `data/YYYY-MM-DD.jsonl`. Ctrl+C to stop.

`fetch_admin_data.py` covers `GetBadgeScanData`/`GetRidershipData` separately — those need a date range and access is unconfirmed on this deployment, so they're not wired into the loop.

**3. Flatten (after collecting)**
```
python3 flatten.py
```
Explodes the raw JSONL into flat CSVs anyone can open in Excel/Sheets or load with one line of pandas/DuckDB: `positions.csv`, `capacities.csv`, `vehicle_estimates.csv`, `stop_estimates.csv`, `stop_arrivals.csv`. Raw JSONL stays as the source of truth — this is a read-only projection of it, safe to regenerate anytime.

**4a. Detect stop visits**
```
python3 arrivals.py
```
Answers "when did a bus actually reach a stop, and when did it leave." Watches each vehicle's currently-targeted stop (`vehicle_estimates.csv`) for when the target changes, and within that window uses `GroundSpeed` to find when it was actually stopped, not just nearby. Writes `data/visits.csv`: one row per stop visit, with `dwell_seconds` (how long it sat there) and `travel_to_next_seconds` (time from leaving this stop to arriving at the next).

Known limitation: a bus stuck in traffic while still approaching a stop can register as "stopped" too early, inflating that visit's dwell time. If this turns out to matter, the fix is cross-checking GPS distance to the stop's real lat/lon (in `reference/per_route.json`) instead of relying on speed alone — not built yet, flagged in `arrivals.py`.

**4b. Summarize**
```
python3 analyze.py
```
Reports vehicle/capacity counts, overall average dwell and inter-stop travel time, and — the specific thing we care about — actual dwell vs. the ~180s expected at the known scheduled-hold stops (North Ave Apts both directions, West Village, Fitten Hall, Weber Loop, GT Competition Center, MARTA Midtown Station, Clough Commons).

Still open for the team: is this dwell/travel data good enough to build a corrected ETA on top of as-is, or does the arrival-detection heuristic need the GPS-distance refinement first? See the comment at the bottom of `analyze.py`.

Related issues: gtiosclub/Georgia-Tech-App#237-#241.
