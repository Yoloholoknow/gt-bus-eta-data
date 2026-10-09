# gt-bus-eta-data

Collecting GT bus data (`bus.gatech.edu`, no API key needed) and figuring out, as a team, how to analyze it toward better ETA predictions.

## Setup

```
uv sync
```

## Pipeline

Four stages, run in order:

**1. Reference data (once)**
```
python3 -m gtbus.reference
```
Routes, stops, markers, route schedules, map config — the stuff that barely changes. Saved to `reference/`.

**2. Poll (continuously, for the collection window)**
```
python3 -m gtbus.collect.raw_poller
```
Every 5s, hits every live-changing endpoint: vehicle positions, stop estimates (both the per-route and per-vehicle shapes), vehicle capacities, stop arrival times, Twitter feed. Appends one JSON line per poll to `data/YYYY-MM-DD.jsonl`. Ctrl+C to stop.

`GetBadgeScanData` returns `200 []` (no data configured) and `GetRidershipData` times out, so neither is polled; `GetVehicleCapacities` already gives a live crowding signal.

**2b. Vehicle heading collector and dashboard (optional)**
```
python3 -m gtbus.collect.vehicle_heading
streamlit run gtbus/dashboard/app.py
```

The heading collector writes one row per vehicle per poll to `data/vehicle_heading/vehicle_points_YYYY-MM-DD.csv`. The dashboard shows the latest bus map, active-bus counts, route speed summaries, heading distributions, source age, and polling gaps.

**3. Flatten (after collecting)**
```
python3 -m gtbus.analysis.flatten
```
Explodes the raw JSONL into flat CSVs anyone can open in Excel/Sheets or load with one line of pandas/DuckDB: `positions.csv`, `capacities.csv`, `vehicle_estimates.csv`, `stop_estimates.csv`, `stop_arrivals.csv`. Raw JSONL stays as the source of truth — this is a read-only projection of it, safe to regenerate anytime.

**4a. Detect stop visits**
```
python3 -m gtbus.analysis.arrivals
```
Answers "when did a bus actually reach a stop, and when did it leave." Watches each vehicle's currently-targeted stop (`vehicle_estimates.csv`) for when the target changes, and within that window uses `GroundSpeed` to find when it was actually stopped, not just nearby. Writes `data/visits.csv`: one row per stop visit, with `dwell_seconds` (how long it sat there) and `travel_to_next_seconds` (time from leaving this stop to arriving at the next).

Known limitation: a bus stuck in traffic while still approaching a stop can register as "stopped" too early, inflating that visit's dwell time. If this turns out to matter, the fix is cross-checking GPS distance to the stop's real lat/lon (in `reference/per_route.json`) instead of relying on speed alone — not built yet, flagged in `gtbus/analysis/arrivals.py`.

**4b. Summarize**
```
python3 -m gtbus.analysis.summary
```
Reports vehicle/capacity counts, overall average dwell and inter-stop travel time, and — the specific thing we care about — actual dwell vs. the ~180s expected at the known scheduled-hold stops (North Ave Apts both directions, West Village, Fitten Hall, Weber Loop, GT Competition Center, MARTA Midtown Station, Clough Commons).

Still open for the team: is this dwell/travel data good enough to build a corrected ETA on top of as-is, or does the arrival-detection heuristic need the GPS-distance refinement first? See the comment at the bottom of `gtbus/analysis/summary.py`.

Related issues: gtiosclub/Georgia-Tech-App#237-#241.


## Layout

```
gtbus/
  api.py, paths.py        shared API client + repo paths
  reference.py            fetch static routes/stops -> reference/
  collect/                raw_poller (JSONL), vehicle_heading (CSV), stop_visits (CSV)
  analysis/               flatten -> arrivals -> summary
  dashboard/app.py        Streamlit dashboard
reference/                static route/stop JSON + all_stops.json (stop_visits config)
data/                     collected data: vehicle_heading/ and stops_timings/ CSVs are
                          committed snapshots; raw *.jsonl and flattened top-level *.csv are gitignored
```

Fly runs `vehicle_heading` and `stop_visits` (see `Dockerfile`).

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

**Pull the volume locally** (machine must be running). `data/` is both the Fly volume mount (`/app/data`) and the committed local snapshot (formerly `data-backup/`), so pulling overwrites local files with the live volume. Review `git status` before committing.
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