import glob
import json

import pandas as pd


def load_polls():
    rows = []
    for path in sorted(glob.glob("data/*.jsonl")):
        with open(path) as f:
            rows.extend(json.loads(line) for line in f)
    return rows


def to_frame(rows, key):
    return pd.DataFrame(
        item | {"fetched_at": r["fetched_at"]}
        for r in rows
        for item in r[key]
    )


def summarize_vehicles(df):
    print(f"\n[vehicles] {len(df)} rows, {df['VehicleID'].nunique()} unique vehicles")
    print(df.groupby("RouteID").size())


def summarize_capacities(df):
    print(f"\n[capacities] {len(df)} rows")
    print(f"avg occupancy: {(df['Percentage'].mean() * 100):.1f}%")


def summarize_estimates(df):
    print(f"\n[estimates] {len(df)} route-poll snapshots")


# --- Below this line: not implemented, this is what needs deciding as a team ---
#
# 1. Ground truth: how do we turn the estimate stream into an actual
#    arrival timestamp per vehicle/stop visit? (last estimate before
#    it disappears vs. watching a flag flip vs. GPS radius crossing)
#
# 2. Target metric: predicted-vs-actual arrival error — grouped by what?
#    (route? stop? hour of day? something else?)
#
# 3. Known long-dwell stops (North Ave Apts, West Village, Fitten Hall,
#    Weber Loop, GT Competition Center, MARTA Midtown, Clough Commons)
#    expect ~3min holds — how should that factor into the estimate?
#
# def compute_actual_arrivals(estimates_df): ...
# def compute_prediction_error(arrivals_df): ...
# def report(error_df): ...


if __name__ == "__main__":
    rows = load_polls()
    print(f"{len(rows)} polls loaded")

    if rows:
        summarize_vehicles(to_frame(rows, "vehicles"))
        summarize_capacities(to_frame(rows, "capacities"))
        summarize_estimates(to_frame(rows, "estimates"))
