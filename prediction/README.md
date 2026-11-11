# Arrival averages and live evaluation

There are two pipeline scripts. Both use Python 3.9+ and the standard library;
they do not require pandas, Streamlit, or additional packages.

Run these commands from the repository root, using the existing project
interpreter. Its HTTPS certificate store works with the GT API; the system
Python 3.13 installation currently has no configured CA certificates. Any
Python 3.9+ installation with a working certificate store is also suitable.
Certificate verification remains enabled.

## 1. Build the frozen averages

```sh
.venv/bin/python prediction/build_predictions.py
```

This reads **all valid historical visits**, without a training/validation split,
and writes `prediction/averages.csv`. The default continuity limit is **60
minutes between consecutive stops**, as requested. Change it with
`--max-gap-seconds 3600`. Input/output paths are configurable with `--visits`,
`--routes`, and `--output`.

Each mean is an **arrival-to-arrival duration** for one route, origin, and
horizon. It includes dwell at the origin and intermediate stops; it does not
include dwell at the destination. Dwell is not added again. Only complete
observed stop sequences contribute. Missing stops, route changes, nonpositive
intervals, duplicates, and gaps over the configured limit break continuity;
real delays within that limit remain in the averages.

Every route with N ordered stops gets horizons **1 through N**: the next stop
through the next full circuit. Repeated RouteStopIDs within one route are
rejected because the historical CSV cannot disambiguate their occurrences.

The CSV contains no-bucket means (`bucket_minutes=0`) and means for 30, 60, 90,
120, 150, 180, 210, and 240 minutes. Buckets use **origin arrival time in
America/New_York**, aligned to midnight. Means include sample/date counts and
a hash tying them to the saved route definition. Averages that cannot be
estimated from complete journeys are absent, not filled with invented times.

## 2. Run the continuous evaluator

```sh
.venv/bin/python prediction/eval_loop.py --run-dir data/prediction_eval/my-run
```

Keep the terminal running. **Press Ctrl+C whenever you want to stop.** Run the
same command to resume. The averages stay frozen; live observations are never
added to training. Rebuilding/changing the averages or labeling settings
requires a new run directory. Omitting `--run-dir` creates a timestamped run.

For a bounded collection, add `--duration-seconds 60`. The loop has no required
minimum duration. Longer collection provides more samples, particularly at
long horizons and on less frequent routes.

### Files updated during collection

| File in the run directory | Contents/update behavior |
| --- | --- |
| `predictions.csv` | Every origin-triggered forecast, appended immediately, including unavailable variants. |
| `arrivals.csv` | Each fresh, bracketed GPS geofence entry. |
| `eval_results.csv` | One finalized row per forecast: resolved, excluded, or unavailable. Completed arrivals are flushed to disk immediately. |
| `eval_summary.csv` | Cumulative metrics by route, horizon, bucket variant, and common one-hour reporting groups; replaced atomically after forecast/result changes. |
| `bucket_comparison.csv` | Best observed bucket per route and paired day-bootstrap intervals; refreshed each minute and on shutdown. |
| `raw_observations.jsonl` | Timestamped API responses and collection-health ticks. |
| `checkpoint.sqlite3` | Internal durable state, pending forecasts, and authoritative raw/results journal. Keep it for resuming or recovering interrupted CSV writes. |
| `diagnostics.csv` | Fresh/duplicate fixes, ambiguous matches, rejected other-direction candidates, arrivals, forecast outcomes, and exclusion reasons. Updated continuously. |
| `detector_events.csv` | Timestamped route-progress decisions for inspecting accepted arrivals, rejected candidates, and unobserved destinations. |
| `replay_manifest.json` | Source snapshot cutoff, model hash, detector version, and replay completion time. |
| `collector.pid` | Parent process ID while live collection is running. Removed on clean shutdown. |

CSVs have headers even before data arrives. The append-only prediction log
and the result, summary, and bucket-comparison CSVs include `route_color`
(the human-readable route label, such as Red, Blue, or Gold) alongside
`route_id`. Existing checkpoint rows receive this label when CSVs are rebuilt
on restart. Non-color routes use their route names.

The append-only prediction log
records forecasts when issued; current statuses live in the results/summary
and checkpoint. Unresolved forecasts are **pending**, not zero-error results.
Ctrl+C stops new requests, commits in-flight responses, and refreshes summaries;
shutdown can take up to the request timeout. If a process is killed during a
CSV write, restart with the same run directory to regenerate valid CSVs from
the checkpoint. Two processes cannot use the same run directory concurrently.

### Prediction and outcome rules

The collector requests GPS every approximately **3 seconds** and estimates
every approximately **10 seconds**, with one outstanding request per endpoint,
total request deadlines, and error backoff. Each endpoint runs in a separate
worker process that can be terminated and restarted if DNS, TLS, or a read
blocks. Gap detection continues while requests are in flight. It batches route-stop IDs into one estimates
request. It uses `GetMapVehiclePoints` and `GetStopArrivalTimes` on the existing
GT TransLoc deployment. These intervals are configurable with `--gps-seconds`
and `--estimate-seconds`.

An origin event is a fresh GPS fix entering a **10-meter geofence** after an
outside fix on the correct portion of the route. Stop anchors are jointly
assigned in route order around one polyline circuit, within 25 meters of the
path. Geometry that cannot satisfy this is reported as unusable for that route.
The **40-meter route corridor** is configurable with `--route-corridor-meters`.

A bounded set of candidate path positions tracks forward progress using fresh
movement, not stationary heading. Startup and recovery require unambiguous
direction before creating forecasts. Overlapping outbound/return segments do
not automatically count as the same route position. The matching bounds are
35 m/s maximum forward progress, 15 meters of backward GPS jitter, 8 meters of
movement for direction, and 25 meters maximum spread for confident candidate
positions. They are fixed physical matching rules, not tuned to model errors.

Startup-inside observations do not count. A **25-meter exit radius** prevents
boundary jitter from creating another arrival; change it with
`--exit-radius-meters` (it must exceed the entry radius). `IsArriving` is not required for
GPS labels. The label is **observed GPS entry**, a proxy for physical arrival;
arrival rows/results retain the previous observation as a lower timing bound.
Inspect real trajectories before relying on small differences in accuracy.

At each origin event, all nine model variants predict every horizon:

```text
predicted arrival = observed origin arrival + historical mean elapsed time
```

A bucket with fewer than **20 samples across three local dates** falls back
to that route/origin/horizon's no-bucket mean, without pooling other routes.
The fallback and the sample count actually used are logged. With no applicable
mean, the forecast is unavailable. Adjust thresholds with `--min-samples` and
`--min-dates`.

Each vehicle has one next arrival for each destination; later circuits are
not duplicated within a forecast event. A current "at stop" state is distinct
from the full-circuit future arrival. Evaluation keeps forecasts from successive
origin events, since those measure different remaining horizons.

TransLoc forecasts use only responses received by prediction time. Explicit
`EstimateTime` is preferred; numeric `Seconds` is used when it is absent.
Schedule-only entries without a vehicle ID are ignored. For multiple estimates
of the same vehicle/stop, only the nearest applicable occurrence is compared.
For the origin's next circuit, the current occurrence must be identifiable as
`IsArriving` and a later occurrence must exist; ambiguous vendor circuit matches
are not scored. Missing vendor estimates never become arrivals or zero error.

GPS freshness is measured from **local receipt times of advancing source
fixes**, not by subtracting GPS `TimeStamp` from local UTC. This deployment's
GPS clock is offset, and `Seconds` counts down on cached fixes. Source dates
are used only to order and deduplicate GPS fixes; no fixed timezone correction
is applied. TransLoc's absolute arrival estimates retain their separate clock.

The route-aware detector is **version 4**. Its settings, route file hash, and
polyline hash are frozen in each checkpoint. Earlier detector checkpoints
must be replayed into a new directory; no old scores are appended or upgraded
in place. The historical average CSV is unchanged and does not need rebuilding.

The maximum time without an advancing GPS fix, observation gaps, and estimate-cache age each default to
**30 seconds**. Use `--max-gps-age-seconds`, `--max-observation-gap-seconds`, and
`--max-estimate-age-seconds` to change them. `--radius-meters` changes the
geofence. Forecasts spanning GPS outages (including restart gaps), missing
vehicles, route changes, or ambiguous route/circuit transitions are
**excluded with reasons**. They are never silently matched to a later circuit.

Missing an intermediate stop observation does **not** invalidate later targets
when continuous, unambiguous path progress identifies the destination and
circuit. Resolved rows record `intermediate_observations_missing` and
`missed_intermediate_count`. No missing arrival timestamps are fabricated.
If progress passes a destination without observing its arrival, only forecasts
for that occurrence are excluded as `destination_not_observed`; later targets
remain pending. Circuit-specific target sequences prevent substitution of a
later visit to the same physical stop.

### Metrics and interpretation

```text
error = predicted arrival - observed arrival
```

Positive error means a prediction later than observed arrival; negative means
earlier. Summary CSVs contain MAE, median/p90 absolute error, signed bias,
fractions within 30/60/120 seconds, coverage, pending/excluded/unavailable
counts, fallback counts, and paired TransLoc metrics. Fractions are between
0 and 1. Blank metrics mean there are no applicable outcomes yet.
The `complete_observations_*` and `missing_intermediate_*` columns separately
report scored cases with and without missed intermediate arrival observations,
in addition to the combined metrics. Diagnostic forecast counts include all
nine variants; divide by nine for unique journey/horizon counts when comparing
coverage. Fewer detected arrivals can be correct when false return-stop
arrivals have been removed. Always inspect coverage with accuracy.

The `all_variants_common_*` columns compare models on identical resolved
journeys. Paired model/TransLoc MAEs use only rows where both forecasts exist;
positive `paired_improvement_seconds` favors the historical model. Hourly
reporting groups use the origin's local hour, independently of candidate
bucket widths. The detailed result CSV retains actual elapsed time for
analyses by remaining journey duration.

The bucket comparison ranks candidates by **mean MAE across equally weighted
supported horizons**, using the common journey cohort. It lists unsupported
horizons; equal scores prefer no buckets, then the widest bucket. Paired
improvement intervals use 500 whole-local-day bootstrap samples and remain
blank until at least two paired dates exist. The paired improvement pools
paired examples across supported horizons; it is distinct from the
equal-horizon selection score.

This live run both selects and measures the best bucket: results are labeled
**selection-set performance**, not an independent test. Very short runs and
sparse horizons are preliminary, regardless of the reported winner.

### Replay and verification

Replay a checkpoint into a **new** run directory, without API calls. A read
transaction pins a consistent observation snapshot even if the source is active:

```sh
.venv/bin/python prediction/eval_loop.py \
  --run-dir data/prediction_eval/replay \
  --replay data/prediction_eval/my-run/checkpoint.sqlite3
```

JSONL replay remains supported by passing `raw_observations.jsonl`; its initial
byte boundary is pinned and an incomplete trailing line is ignored. Use the
same historical averages and route snapshot when comparing detector versions.
The evaluator rejects observations at or before the training cutoff. To
recalculate saved JSONL observations with newer averages, explicitly add
`--allow-training-overlap` to a replay command and use a fresh run directory:

```sh
.venv/bin/python prediction/eval_loop.py \
  --averages prediction/averages.csv \
  --replay data/prediction_eval/20261019T181853255804Z/raw_observations.jsonl \
  --allow-training-overlap \
  --run-dir data/prediction_eval/20261027_new_predictions_replay_v2
```

This option permits observations at or before the training cutoff. Results
can overlap training data and are not independent test accuracy. The option
is frozen in checkpoint settings and recorded in the replay manifest. It
requires `--replay`; SQLite replay still requires the source model hash to
match, so use the raw JSONL journal when changing averages.

For replays without training overlap, continue collecting into the corrected run using the same
`--run-dir` without `--replay`. Restart gaps still exclude pending forecasts
whose circuit cannot be verified. Start a separate run for live evaluation
after a replay with `--allow-training-overlap`.

Run collection on a machine that stays awake and connected. Check
`diagnostics.csv` for GPS request errors and fresh-fix activity after outages.
When running detached, stop the parent gracefully with
`kill -INT $(cat data/prediction_eval/my-run/collector.pid)` and wait for it to
exit before resuming the same directory. Ctrl+C remains supported in a terminal.

Run the direction, circuit, GPS-clock, worker-timeout, and recovery tests with:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_eval_timestamps.py' -v
```

## Explore predictions in Streamlit

From the repository root, run:

```sh
.venv/bin/python -m streamlit run prediction/dashboard.py
```

Open http://localhost:8501. The dashboard reads existing results and refreshes
every 30 seconds while open; it does not start collection or modify the model.
Select a run, model averaging interval, routes, origin/destination stops,
stops ahead, departure dates, and departure intervals. Model averaging width
and departure-time grouping are independent controls; time groups are aligned
to midnight in America/New_York. Only one model variant is selected at a time.
Sparse models may have used the evaluator's unbucketed fallback; inspect
`fallback_used` and `sample_count` in the individual forecast table.

The distributions and metrics compare exactly the same resolved forecasts
with finite model and TransLoc errors. Negative signed error means early,
positive means late, and zero is the observed arrival. Explore histograms,
cumulative absolute-error curves, grouped box plots, and actual-versus-predicted
elapsed times. The individual view also compares arrival clock times for a
selected forecast and offers a filtered CSV download. Counts include distinct
destination visits because overlapping forecasts are correlated.

Exclusions are shown separately by reason and route. Finalized CSVs do not
contain pending forecasts. Accuracy describes the scoreable subset, and GPS
arrival labels are not verified door-opening times. Keep detector versions in
separate runs when interpreting results.

## Export individual ground truth and compare your own model

Create one row per origin arrival and destination horizon, deduplicating the
nine averaging variants. This reads the checkpoint, including pending journeys,
in a consistent read-only transaction:

```sh
.venv/bin/python prediction/export_ground_truth.py \
  --run-dir data/prediction_eval/20261019T181853255804Z
```

The output is `ground_truth.csv` inside that run. Use `--output PATH` to write
elsewhere. To keep refreshing it while `eval_loop.py` collects in another
terminal, add `--watch` (default refresh: 10 seconds; Ctrl+C stops the export).
The exporter does not start collection. It replaces the output atomically,
so each refresh includes updated statuses without duplicate rows. Re-exporting
the same run preserves sample IDs, independently of which baseline you select.

Each row includes:

- `sample_id`: join your model's prediction to exactly this journey/horizon.
- Route, vehicle, origin/destination IDs and names, and circuit sequences.
- `stops_ahead`, `route_fraction_ahead` (stops ahead divided by route stop count),
  and `full_circuit_ahead`. Fraction means stop count, not physical distance.
- `prediction_time`: the observed origin arrival in UTC; corresponding local
  timestamp, date, weekday (Monday=0), weekend flag, hour, minute, and time bucket
  in America/New_York. Change reporting buckets with `--time-bucket-minutes 30`.
- `actual_arrival`, `actual_elapsed_seconds` (the supervised ETA target), the
  preceding GPS receipt `actual_lower_bound`, elapsed lower bound, and destination
  arrival uncertainty in seconds. Times describe GPS geofence entry, not doors opening.
- Status/exclusion reason, missing intermediate observation flags, detector
  version, frozen baseline hash, baseline elapsed prediction/sample count/fallback,
  and the TransLoc elapsed prediction available when the forecast was issued.

These samples start at observed stop arrivals, not arbitrary GPS times between
stops. `label_available=1` identifies trainable/scorable rows. Pending, excluded,
and unavailable journeys have blank labels, never zero targets. Labels reuse
resolved outcomes from the existing detector. If **every** averaging variant
was unavailable, that journey remains unlabeled even if a later arrival exists:
the evaluator did not track a pending forecast for it. Inspect this coverage
before treating the export as a complete census of bus journeys.

For training, use origin-time features such as route, stop IDs, stops ahead,
weekday and local time. Actual arrival/bounds, outcome status, missing-observation
flags, and target values are future information; do not use them as features.
Split chronologically by date before fitting/tuning, keeping overlapping horizons
from the same origin event together. Baseline scores on data used to train a new
model are not an independent evaluation.

To compare a new model immediately, write a CSV with these two columns and one
row per sample you predict; elapsed seconds are measured from `prediction_time`:

```csv
sample_id,predicted_elapsed_seconds
<sample_id copied from ground_truth.csv>,125.5
```

Then run:

```sh
.venv/bin/python prediction/export_ground_truth.py \
  --run-dir data/prediction_eval/20261019T181853255804Z \
  --predictions /path/to/my_predictions.csv
```

This also writes `ground_truth.scores.csv` (individual signed/absolute errors)
and `ground_truth.metrics.csv` (overall, per route, per route/stops ahead, and per
route/stops ahead/time bucket). Metrics include MAE, median/p90 absolute error,
bias, within-30/60/120-second rates, resolved prediction coverage, and paired
improvement over the frozen baseline. Positive error means late; positive
improvement means your model has lower paired MAE. P90 uses the nearest rank.
Missing predictions are counted in coverage, not assigned zero error. Unknown
prediction IDs are reported in the terminal; duplicate IDs and invalid elapsed
times fail validation. Use `--baseline-bucket-minutes 60` to compare with another
saved averaging variant (default: unbucketed). Only paired cases contribute to
the improvement metric. With `--watch`, the prediction CSV is reread each refresh;
replace your prediction file atomically when updating it.

The saved route file must match the checkpoint's frozen route hash, and the
checkpoint must use detector version 4. Replay older runs first. The export
requires only Python's standard library.

## Live corrected arrivals for a selected stop

Run a local, read-only CLI; no API server or hosting is required:

```sh
.venv/bin/python prediction/live_arrivals.py --route Red --stop "West Village"
```

Route names/colors and route IDs are accepted, case insensitive. A stop can be
its **RouteStopID**, exact name, or an unambiguous part of its name. Ambiguous
names list the matching IDs rather than guessing a direction. For example:

```sh
.venv/bin/python prediction/live_arrivals.py --route Red --stop 273
.venv/bin/python prediction/live_arrivals.py --route Gold --list-stops
.venv/bin/python prediction/live_arrivals.py --route Gold --stop "MARTA Midtown Station" --bucket-minutes 60
```

Each poll is an independent snapshot, with no GPS warmup or background history:

1. Fetch `GetStopArrivalTimes` for the selected route-stop to identify approaching
   vehicles and their original TransLoc arrival estimates.
2. Fetch `GetVehicleRouteStopEstimates` for those vehicle IDs and select each
   vehicle's earliest valid future stop on the selected route.
3. Add the direct historical mean from that future stop to the destination.

The CLI loads `prediction/averages.csv` once and refreshes every five seconds.
The default is unbucketed means (`--bucket-minutes 0`). To use two-hour buckets:

```sh
.venv/bin/python prediction/live_arrivals.py --route Red --stop "West Village" --bucket-minutes 120
```

Bucket selection uses the estimated arrival time at the future anchor in
America/New_York. Sparse buckets use the existing unbucketed fallback rules;
`[unbucketed mean]` marks this explicitly. No evaluation winner is selected
implicitly. Dwell at the model origin/intermediate stops is already included.

Stops marked `IsArriving` are skipped as model anchors: their elapsed dwell is
unknown. TransLoc's ETA to the next future stop covers remaining dwell plus
travel, and the model starts at that stop's estimated arrival. There is no
stop-anchored countdown based on a previously observed GPS arrival anymore.

Sources in the output:

- `hybrid`: TransLoc to the next future stop plus the direct model onward.
  Horizons follow route order, wrapping at the circuit boundary.
- `transloc-next-stop`: the selected destination is the next future stop; retain
  its original selected-stop estimate without adding a model segment.
- `transloc-arriving`: the selected-stop endpoint marks this bus as arriving;
  show the current occurrence with zero remaining seconds, not a later circuit.
- `transloc-fallback`: upcoming estimates are missing/stale, the earliest future
  stop is ambiguous, the vehicle reports another route's stops, the target
  occurrence precedes the anchor, or the required model mean is unavailable.
  The reason is printed below the bus.

Schedule-only and departed entries are excluded. Each vehicle has at most one
output row, representing its nearest selected-stop occurrence. Upcoming entries
are ordered by estimated arrival time rather than assuming API array order.
A response over 30 seconds old is not used (`--max-age-seconds` changes this
limit). This checks response receipt age; it cannot establish whether TransLoc's
underlying observations themselves were stale. Missing upcoming estimates fall
back to TransLoc; a missing/stale selected-stop response produces no bus rows.

Times are minutes:seconds, with arrival clocks in America/New_York. Vehicle IDs
are displayed by default. `--vehicle-names` optionally requests fleet names from
`GetMapVehiclePoints`; this data never determines predictions. If names are slow
or unavailable, vehicle IDs still work. An outstanding optional name request can
finish during shutdown, up to its request timeout.

Press Ctrl+C to stop. `--once` performs one complete snapshot and can produce
hybrid predictions immediately. `--duration-seconds 60` runs for a bounded
period. `--json` prints one JSON object per snapshot, including source, fallback
reason, anchor ID/name, horizon, model sample count, actual model bucket width,
and UTC arrival timestamps. Model and route paths can be changed with
`--averages` and `--routes`.

For an iOS integration, the same calculation can run when the arrivals screen
opens or refreshes, using bundled model/route data and the two API responses.
It needs no continuously running collector or server.

### Editable route/time offsets

`prediction/route_time_offsets.csv` is an editable table of **seconds**, with
route colors/names as rows and ten two-hour columns from `4-6 AM` through
`10 PM-12 AM` (10 PM to midnight). All cells initially contain zero.

The CLI loads this table automatically and reloads it before every poll. Edit a
cell while the CLI runs to use it on the next poll. `--offsets PATH` selects a
different table. Invalid/blank/nonfinite cells and duplicate route rows produce
a clear error instead of silently applying a bad correction.

The **current prediction-request time in America/New_York** selects the column,
independently of the historical model's bucket or predicted arrival time. For
example, setting Red's `4-6 PM` cell to `-60` makes its final predicted arrivals
one minute earlier for snapshots requested from 4 PM up to (not including)
6 PM. `30` makes arrivals 30 seconds later. Route names are case insensitive.
Missing route rows and midnight through 4 AM use zero correction.

Apply the offset once to the final ETA, including hybrid, next-stop, and
TransLoc fallback predictions. The original `TRANSLOC` comparison is unchanged.
Remaining time is clamped to zero; buses already marked `transloc-arriving`
stay at zero. The `OFFSET` display column reports the actual applied change.
JSON includes the configured `offset_seconds`, actual `applied_offset_seconds`,
and `unadjusted_predicted_arrival` per bus, plus the snapshot's `offset_block`
and `route_offset_seconds`. When clamping or arriving prevents the full
correction, configured and applied seconds can differ.

These live hybrid estimates have not yet been independently scored. The existing
evaluator measures stop-origin forecasts, not this between-stop combination.

Offline behavioral checks:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_live_arrivals.py' -v
```
