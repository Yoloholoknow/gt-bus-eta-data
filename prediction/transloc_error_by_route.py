#!/usr/bin/env python3
"""Compare model and TransLoc errors by route and stops ahead.

Usage: python prediction/transloc_error_by_route.py [RUN_DIRECTORY_OR_CSV]
Defaults to the most recently updated eval_results.csv. Reads only; safe to
run while collection continues. Uses bucket_minutes=0 to count each forecast
once across model variants, and only resolved forecasts with a TransLoc ETA.
"""

import argparse
import csv
import io
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean


ROOT = Path(__file__).resolve().parents[1]
BANDS = ((1, 1, "1"), (2, 3, "2–3"), (4, 6, "4–6"),
         (7, 10, "7–10"), (11, math.inf, "11+"))


def percentile(values, fraction):
    """Linear interpolation between adjacent sorted observations."""
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def table_row(label, rows):
    if not rows:
        return [label, "0", "0"] + ["—"] * 7
    model_errors = [float(row["error_seconds"]) for row in rows]
    vendor_errors = [float(row["transloc_error_seconds"]) for row in rows]
    model_absolute = [abs(error) for error in model_errors]
    vendor_absolute = [abs(error) for error in vendor_errors]
    model_mae = mean(model_absolute)
    vendor_mae = mean(vendor_absolute)
    arrivals = {(row["vehicle_id"], row["destination_route_stop_id"],
                 row["actual_arrival"]) for row in rows}
    return [label, str(len(rows)), str(len(arrivals)),
            f"{model_mae:.1f}", f"{vendor_mae:.1f}",
            f"{vendor_mae - model_mae:+.1f}",
            f"{mean(model_errors):+.1f}", f"{mean(vendor_errors):+.1f}",
            f"{percentile(model_absolute, 0.9):.1f}",
            f"{percentile(vendor_absolute, 0.9):.1f}"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path,
                        help="Run directory or eval_results.csv (default: latest updated run)")
    args = parser.parse_args()
    source = args.source
    if source is None:
        candidates = list((ROOT / "data" / "prediction_eval").glob("*/eval_results.csv"))
        if not candidates:
            parser.error("No eval_results.csv found; supply a run directory or CSV path.")
        source = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    if source.is_dir():
        source /= "eval_results.csv"
    try:
        # Pin the size on this open file, including if the collector replaces
        # its export concurrently. Ignore an unfinished trailing append.
        with source.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(0)
            content = handle.read(size)
        content = content[:content.rfind(b"\n") + 1]
        reader = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""))
        required = {"route_id", "bucket_minutes", "horizon_stops", "status",
                    "error_seconds", "transloc_error_seconds", "vehicle_id",
                    "destination_route_stop_id", "actual_arrival"}
        if not required.issubset(reader.fieldnames or []):
            parser.error("CSV is missing required eval_results columns.")
        groups = defaultdict(list)
        labels = {}
        for row in reader:
            if (row["bucket_minutes"] != "0" or row["status"] != "resolved"
                    or not row["transloc_error_seconds"] or not row["error_seconds"]):
                continue
            if not all(math.isfinite(float(row[key]))
                       for key in ("error_seconds", "transloc_error_seconds")):
                continue
            rid = row["route_id"]
            labels[rid] = row.get("route_color") or rid
            groups[rid].append(row)
    except (OSError, UnicodeError, ValueError, csv.Error) as error:
        parser.error(str(error))

    print(f"Source: {source.resolve()}")
    print("Seconds; signed error = predicted − observed (negative = early).")
    print("Your model = no-bucket historical average (bucket_minutes=0).")
    print("Paired comparison: both errors use exactly the same resolved forecasts.")
    print("MAE gain = TransLoc MAE − your MAE; positive favors your model.")
    print("Arrivals are distinct destination visits; forecasts are correlated.")
    print("Includes directly observed destinations with missed intermediate stops.")
    if not groups:
        print("\nNo eligible resolved TransLoc forecasts yet.")
    for rid in sorted(groups, key=lambda key: (labels[key].casefold(), key)):
        rows = groups[rid]
        output = [["Stops ahead", "Forecasts", "Arrivals", "Your MAE", "TransLoc MAE",
                   "MAE gain", "Your bias", "TransLoc bias", "Your P90", "TransLoc P90"]]
        for low, high, label in BANDS:
            subset = [row for row in rows if low <= int(row["horizon_stops"]) <= high]
            output.append(table_row(label, subset))
        output.append(table_row("All", rows))
        widths = [max(len(row[index]) for row in output) for index in range(len(output[0]))]
        print(f"\n{labels[rid]} (route {rid})")
        for index, row in enumerate(output):
            print(" | ".join(value.ljust(width) for value, width in zip(row, widths)))
            if index == 0:
                print("-+-".join("-" * width for width in widths))


if __name__ == "__main__":
    main()
