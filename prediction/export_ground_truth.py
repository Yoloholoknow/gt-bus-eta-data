"""Export individual evaluator journeys, optionally watching and scoring a model.

Reads a consistent, read-only checkpoint snapshot. Requires only the standard
library; does not collect GPS or modify the evaluator's checkpoint.
"""

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

try:
    from .build_predictions import BUCKETS, LOCAL, ROOT, atomic_csv, load_routes, parse_time
except ImportError:
    from build_predictions import BUCKETS, LOCAL, ROOT, atomic_csv, load_routes, parse_time

COLUMNS = [
    "sample_id", "origin_event_id", "route_id", "route_color", "vehicle_id", "vehicle_name",
    "origin_route_stop_id", "origin_stop_name", "destination_route_stop_id", "destination_stop_name",
    "stops_ahead", "route_stop_count", "route_fraction_ahead", "full_circuit_ahead",
    "origin_sequence", "target_sequence", "prediction_time", "prediction_time_local",
    "service_date", "weekday", "is_weekend", "local_hour", "local_minute_of_day",
    "time_bucket_start_minute", "status", "reason", "label_available",
    "actual_arrival", "actual_lower_bound", "actual_elapsed_seconds",
    "actual_elapsed_lower_seconds", "arrival_uncertainty_seconds",
    "intermediate_observations_missing", "missed_intermediate_count",
    "baseline_bucket_minutes", "baseline_predicted_elapsed_seconds", "baseline_fallback_used",
    "baseline_sample_count", "transloc_predicted_elapsed_seconds", "transloc_status",
    "detector_version", "model_hash",
]
SCORE_COLUMNS = ["sample_id", "status", "prediction_available", "predicted_elapsed_seconds",
                 "actual_elapsed_seconds", "error_seconds", "absolute_error_seconds"]
SUMMARY_COLUMNS = ["scope", "route_id", "stops_ahead", "time_bucket_start_minute",
                   "journey_count", "resolved_count", "predicted_count", "scored_count",
                   "resolved_prediction_coverage", "mae_seconds", "median_absolute_error_seconds",
                   "p90_absolute_error_seconds", "bias_seconds", "within_30_seconds",
                   "within_60_seconds", "within_120_seconds", "baseline_paired_count",
                   "baseline_paired_mae_seconds", "model_paired_mae_seconds",
                   "paired_improvement_seconds"]


def snapshot(checkpoint):
    db = sqlite3.connect(Path(checkpoint).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("BEGIN")
        metadata = dict(db.execute("SELECT key, value FROM metadata"))
        frozen = json.loads(metadata["frozen"])
        if frozen.get("format_version") != 4:
            raise ValueError("Ground truth requires detector version 4; replay older runs first")
        cutoff = db.execute("SELECT COALESCE(MAX(id), 0) FROM observations").fetchone()[0]
        rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM predictions ORDER BY rowid")]
        return frozen, cutoff, rows
    finally:
        db.close()


def journeys(frozen, forecasts, routes, bucket_minutes=60, baseline_bucket=0):
    groups = defaultdict(list)
    for row in forecasts:
        groups[(row["origin_event_id"], int(row["horizon_stops"]))].append(row)
    namespace = json.dumps(frozen, sort_keys=True)
    for (_, horizon), variants in groups.items():
        # Labels may exist on another variant even if the chosen baseline is unavailable.
        statuses = {"resolved": 0, "pending": 1, "excluded": 2, "unavailable": 3}
        row = min(variants, key=lambda r: statuses[r["status"]])
        baseline = next((r for r in variants if int(r["bucket_minutes"]) == baseline_bucket), {})
        route = routes[row["route_id"]]
        stops = {s["id"]: s for s in route["stops"]}
        at = parse_time(row["origin_arrival"])
        local = at.astimezone(LOCAL)
        minute = local.hour * 60 + local.minute
        identity = [namespace, row["origin_event_id"], row["origin_arrival"], row["vehicle_id"], horizon]
        result = {k: row.get(k, "") for k in COLUMNS}
        resolved = row["status"] == "resolved"
        result.update(
            sample_id=hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
            route_color=row.get("route_color") or route["name"],
            origin_stop_name=stops[row["origin_route_stop_id"]]["name"],
            destination_stop_name=stops[row["destination_route_stop_id"]]["name"],
            stops_ahead=horizon, route_stop_count=len(stops), route_fraction_ahead=horizon / len(stops),
            full_circuit_ahead=int(horizon == len(stops)), prediction_time=row["origin_arrival"],
            prediction_time_local=local.isoformat(), service_date=local.date().isoformat(),
            weekday=local.weekday(), is_weekend=int(local.weekday() >= 5), local_hour=local.hour,
            local_minute_of_day=minute, time_bucket_start_minute=minute // bucket_minutes * bucket_minutes,
            label_available=int(resolved), baseline_bucket_minutes=baseline_bucket,
            baseline_predicted_elapsed_seconds=baseline.get("mean_elapsed_seconds", ""),
            baseline_fallback_used=baseline.get("fallback_used", ""),
            baseline_sample_count=baseline.get("sample_count", ""),
            detector_version=frozen["format_version"],
        )
        vendor = row.get("transloc_predicted_arrival")
        result["transloc_predicted_elapsed_seconds"] = (parse_time(vendor) - at).total_seconds() if vendor else ""
        if resolved:
            actual = parse_time(row["actual_arrival"])
            lower = parse_time(row["actual_lower_bound"])
            result["actual_elapsed_lower_seconds"] = (lower - at).total_seconds()
            result["arrival_uncertainty_seconds"] = (actual - lower).total_seconds()
        else:
            for key in ("actual_arrival", "actual_lower_bound", "actual_elapsed_seconds",
                        "intermediate_observations_missing", "missed_intermediate_count"):
                result[key] = ""
        yield result


def read_predictions(path):
    predictions = {}
    with Path(path).open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if not {"sample_id", "predicted_elapsed_seconds"}.issubset(reader.fieldnames or []):
            raise ValueError("Predictions require sample_id,predicted_elapsed_seconds columns")
        for row in reader:
            sid = row["sample_id"].strip()
            if not sid or sid in predictions:
                raise ValueError(f"Empty or duplicate sample_id: {sid!r}")
            value = float(row["predicted_elapsed_seconds"])
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Prediction must be finite and nonnegative: {sid}")
            predictions[sid] = value
    return predictions


def score(rows, predictions):
    details, groups = [], defaultdict(list)
    for row in rows:
        value = predictions.get(row["sample_id"])
        error = value - float(row["actual_elapsed_seconds"]) if value is not None and row["label_available"] else ""
        detail = dict(sample_id=row["sample_id"], status=row["status"], prediction_available=int(value is not None),
                      predicted_elapsed_seconds=value if value is not None else "",
                      actual_elapsed_seconds=row["actual_elapsed_seconds"], error_seconds=error,
                      absolute_error_seconds=abs(error) if error != "" else "")
        details.append(detail)
        keys = [("overall", "", "", ""), ("route", row["route_id"], "", ""),
                ("route_horizon", row["route_id"], row["stops_ahead"], ""),
                ("route_horizon_time", row["route_id"], row["stops_ahead"], row["time_bucket_start_minute"])]
        for key in keys:
            groups[key].append((row, detail))
    summaries = []
    for key, pairs in groups.items():
        errors = [d["error_seconds"] for r, d in pairs if d["error_seconds"] != ""]
        absolute = sorted(abs(e) for e in errors)
        resolved = sum(r["label_available"] for r, d in pairs)
        paired = [(float(r["baseline_predicted_elapsed_seconds"]) - float(r["actual_elapsed_seconds"]), d["error_seconds"])
                  for r, d in pairs if d["error_seconds"] != "" and r["baseline_predicted_elapsed_seconds"] != ""]
        baseline_mae = statistics.mean(abs(b) for b, m in paired) if paired else ""
        model_mae = statistics.mean(abs(m) for b, m in paired) if paired else ""
        summaries.append(dict(zip(SUMMARY_COLUMNS, [
            *key, len(pairs), resolved, sum(d["prediction_available"] for r, d in pairs), len(errors),
            len(errors) / resolved if resolved else "", statistics.mean(absolute) if errors else "",
            statistics.median(absolute) if errors else "",
            absolute[math.ceil(.9 * len(absolute)) - 1] if errors else "",
            statistics.mean(errors) if errors else "",
            *[sum(e <= threshold for e in absolute) / len(errors) if errors else "" for threshold in (30, 60, 120)],
            len(paired), baseline_mae, model_mae, baseline_mae - model_mae if paired else "",
        ])))
    return details, summaries


def export(args):
    frozen, cutoff, forecasts = snapshot(args.run_dir / "checkpoint.sqlite3")
    if hashlib.sha256(args.routes.read_bytes()).hexdigest() != frozen["routes_hash"]:
        raise ValueError("Routes must match the checkpoint's frozen route file")
    rows = list(journeys(frozen, forecasts, load_routes(args.routes), args.time_bucket_minutes, args.baseline_bucket_minutes))
    predictions = read_predictions(args.predictions) if args.predictions else None
    details, summaries = score(rows, predictions) if predictions is not None else ([], [])
    atomic_csv(args.output, COLUMNS, rows)
    if predictions is not None:
        atomic_csv(args.output.with_suffix(".scores.csv"), SCORE_COLUMNS, details)
        atomic_csv(args.output.with_suffix(".metrics.csv"), SUMMARY_COLUMNS, summaries)
    counts = Counter(r["status"] for r in rows)
    print(f"{args.output}: {len(rows)} journeys; {dict(counts)}", flush=True)
    if predictions is not None:
        known = {r["sample_id"] for r in rows}
        print(f"Scored {sum(d['error_seconds'] != '' for d in details)}; "
              f"unmatched prediction IDs: {len(predictions.keys() - known)}", flush=True)
    return cutoff


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--routes", type=Path, default=ROOT / "reference/routes.json")
    parser.add_argument("--output", type=Path, help="Default: RUN_DIR/ground_truth.csv")
    parser.add_argument("--watch", action="store_true", help="Refresh until Ctrl+C; collector must run separately")
    parser.add_argument("--interval-seconds", type=float, default=10)
    parser.add_argument("--time-bucket-minutes", type=int, default=60)
    parser.add_argument("--baseline-bucket-minutes", type=int, choices=BUCKETS, default=0)
    parser.add_argument("--predictions", type=Path, help="CSV with sample_id,predicted_elapsed_seconds")
    args = parser.parse_args()
    args.output = args.output or args.run_dir / "ground_truth.csv"
    if not math.isfinite(args.interval_seconds) or args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive and finite")
    if not 1 <= args.time_bucket_minutes <= 1440:
        parser.error("--time-bucket-minutes must be between 1 and 1440")
    # Keep generated files separate from every source, even with an accidental output argument.
    sources = {args.routes.resolve(), (args.run_dir / "checkpoint.sqlite3").resolve()}
    sources.update((args.run_dir / name).resolve() for name in (
        "predictions.csv", "arrivals.csv", "eval_results.csv", "eval_summary.csv",
        "bucket_comparison.csv", "raw_observations.jsonl", "diagnostics.csv",
        "detector_events.csv", "replay_manifest.json", "collector.pid",
        "checkpoint.sqlite3-wal", "checkpoint.sqlite3-shm"))
    if args.predictions:
        sources.add(args.predictions.resolve())
    outputs = [args.output, args.output.with_suffix(".scores.csv"), args.output.with_suffix(".metrics.csv")]
    if len({p.resolve() for p in outputs}) != 3 or any(p.resolve() in sources for p in outputs):
        parser.error("Output paths must be distinct from each other and source files")
    try:
        while True:
            export(args)
            if not args.watch:
                break
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
