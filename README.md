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

`fetch_admin_data.py` covers `GetBadgeScanData`/`GetRidershipData` separately. Tested live: `GetBadgeScanData` returns `200 []` (works, but no data configured on this deployment), `GetRidershipData` times out entirely (broken, doesn't respond). Neither is needed anyway — they're fare-tap/ridership counts, not location/timing data, and `GetVehicleCapacities` (in the main loop) already gives a live crowding signal. Kept in the repo for reference only, not part of the pipeline.

**2b. Vehicle heading collector and dashboard (optional)**
```
python3 vehicle_heading_collector.py
streamlit run dashboard.py
```

The heading collector writes one row per vehicle per poll to `data/vehicle_heading/vehicle_points_YYYY-MM-DD.csv`. The dashboard reads those files and is intended for the vehicle-heading visualization task. It does not replace the raw JSONL collector or the stop-ETA/stop-visit pipeline.

## Vehicle heading dashboard

Run the dashboard from the repository root:

```bash
python3 -m venv .venv                 # only needed for a new checkout
source .venv/bin/activate
pip install -r requirements.txt
streamlit run dashboard.py
```

The dashboard opens at `http://localhost:8501`. In this checkout, the existing `venv/` can also be used:

```bash
source venv/bin/activate
streamlit run dashboard.py
```

### Data used

The dashboard loads `data/vehicle_heading/vehicle_points_*.csv`. Each row represents one vehicle returned by one `GetMapVehiclePoints` poll. The raw fields are:

| Field | Meaning |
| --- | --- |
| `tick` | Collector poll sequence number |
| `fetched_at` | UTC time the collector made the request |
| `source_timestamp` | Timestamp supplied by the vehicle-position API |
| `seconds` | API-reported age of the source position |
| `vehicle_id`, `vehicle_name` | Vehicle identifiers |
| `route_id` | Route assigned to the vehicle in that response |
| `latitude`, `longitude` | Reported vehicle position |
| `ground_speed` | API-reported ground speed |
| `heading` | API-provided compass heading, in degrees clockwise from north |

The dashboard defaults to the complete October 2–8 collection window and excludes October 9 because that collection was incomplete. The date filter can be changed when other local files are available.

### What the dashboard covers

| Section | What it answers |
| --- | --- |
| Filters | Which dates, routes, and vehicles are being analyzed |
| Metric cards | Coverage, vehicle/route counts, polling interval, long gaps, heading comparisons, and high-confidence heading counts |
| Latest bus positions | Where each bus was at a selected recorded poll; route lines are colored and bus arrows show the best available heading |
| Playback timeline | How bus positions and headings changed throughout the recorded window |
| Active buses over time | Whether the collector saw a normal number of buses over time |
| Average speed by route | Which routes had higher or lower reported speed; this is not a direction measurement |
| Inferred heading distribution | The overall travel-direction distribution after applying movement-based fallback logic |
| Source age | How old the upstream API position was when returned |
| API heading versus movement heading | Whether the API heading agrees with the bearing calculated from consecutive GPS positions for a vehicle |
| Direction mix by route | The inferred compass-direction mix for each route |
| Heading agreement | Counts agreement within 30°, partial agreement within 90°, and disagreement above 90° |
| Motion state and heading confidence | How many observations are moving/stopped and whether the displayed heading is current, recent, old, or API-only |
| Route-sequence direction | A heuristic estimate of whether the bus is traveling toward the next or previous stop in `reference/routes.json` |
| Heading age while stopped | How long a stopped bus has been relying on its last movement heading |
| Route summary | Per-route coverage, speed, source age, distance to stops, movement share, comparison coverage, and disagreement share |

### How heading inference works

The API value is preserved as `heading`. The dashboard adds derived fields:

1. Rows are ordered by `vehicle_id` and `fetched_at`.
2. The dashboard calculates the distance and compass bearing from the previous position for that vehicle.
3. A movement bearing is accepted when the previous position is no more than 120 seconds old and the bus moved at least 10 meters.
4. A bus is labeled `Moving` when its reported speed is at least 3 API speed units or its position changed enough to produce a movement bearing. Otherwise, with a valid recent poll, it is labeled `Stopped`.
5. `inferred_heading` uses the current movement bearing first, then the last movement bearing, then the API heading as a final fallback.
6. `heading_confidence` is `High` for a current movement bearing, `Medium` for a recent carried-forward movement bearing, and `Low` for an older or API-only fallback.
7. The API heading and movement heading are compared using the smallest circular difference, so 359° and 1° are treated as 2° apart rather than 358° apart.
8. Each position is matched to the nearest stop for its route. The inferred heading is compared with the bearings toward the neighboring stops to estimate `Toward next stop` or `Toward previous stop`.

### Important limitations

- Latitude/longitude history can estimate travel direction, but it cannot prove which way the physical front of a stationary bus is pointed.
- A stopped bus uses its last reliable movement heading. The dashboard shows the heading age and confidence so that fallback is visible.
- The route-sequence direction is a heuristic based on nearest-stop matching and the ordered stop reference. It can be ambiguous near loops, terminals, overlapping route segments, or inaccurate GPS points.
- The API heading is still useful as an independent signal. The agreement chart is intended to show when it is fresh and trustworthy versus stale or noisy.
- Poll gaps reduce the precision of movement-derived headings. Investigate unusually long gaps before treating a route or vehicle as ground truth.
- The large CSV files under `data/` are ignored by Git. Share or archive them separately rather than committing the collection data to the code branch.

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


## Fly

The collectors run on one Fly machine (app `gt-bus-eta-data`, config in `fly.toml`). Data lives on the `data` volume, mounted at `/app/data`. Requires [flyctl](https://fly.io/docs/flyctl/install/) and `fly auth login`.

**Deploy / update**
```
fly deploy
```

**Check status and logs**
```
fly status -a gt-bus-eta-data
fly logs -a gt-bus-eta-data
```

**Stop / start the machine** (the volume and its data are kept while stopped)
```
fly machine list -a gt-bus-eta-data          # get the machine ID
fly machine stop <machine-id> -a gt-bus-eta-data
fly machine start <machine-id> -a gt-bus-eta-data
```
The restart policy is `always`, so a stopped machine only stays down if you stop it explicitly; a crash or `fly deploy` brings it back up.

**Pull the volume locally** (machine must be running)
```
fly ssh sftp get -R -a gt-bus-eta-data /app/data ./data
```
Or as a single archive:
```
fly ssh console -a gt-bus-eta-data -C "tar czf - -C /app data" > data.tgz
```

**Look around on the machine**
```
fly ssh console -a gt-bus-eta-data
```
