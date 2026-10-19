"""Read-only evaluation dashboard: streamlit run prediction/dashboard.py."""

import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "data" / "prediction_eval"
COLORS = {"Your model": "#2563eb", "TransLoc": "#f97316"}


@st.cache_data(max_entries=3, ttl=30)
def load_results(path, modified):
    # Read the file size once; a concurrent append cannot leave a partial row.
    with Path(path).open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(0)
        raw = handle.read(size)
    raw = raw[:raw.rfind(b"\n") + 1]
    frame = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
    for column in ("intermediate_observations_missing", "missed_intermediate_count"):
        if column not in frame:
            frame[column] = ""  # Older detectors did not record this information.
    for column in ("error_seconds", "transloc_error_seconds", "horizon_stops",
                   "bucket_minutes", "bucket_start_minute", "sample_count"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("origin_arrival", "actual_arrival", "predicted_arrival",
                   "transloc_predicted_arrival", "finished_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    local = frame["origin_arrival"].dt.tz_convert("America/New_York")
    frame["Departure date"] = local.dt.strftime("%Y-%m-%d")
    frame["departure_minute"] = local.dt.hour * 60 + local.dt.minute
    frame["Route"] = frame.get("route_color", frame["route_id"])
    frame["Route"] = frame["Route"].where(frame["Route"] != "", frame["route_id"])
    names = {}
    route_file = ROOT / "reference" / "routes.json"
    if route_file.exists():
        for route in json.loads(route_file.read_text()):
            for stop in route.get("Stops", []):
                names[(str(route["RouteID"]), str(stop["RouteStopID"]))] = str(
                    stop.get("Description") or stop.get("Name") or stop["RouteStopID"])
    for column, title in (("destination_route_stop_id", "Destination"),
                          ("origin_route_stop_id", "Origin")):
        frame[title] = [f"{names.get((rid, sid), sid)} [{sid}]"
                        for rid, sid in zip(frame["route_id"], frame[column])]
    return frame


def clock_label(minute):
    return f"{int(minute) // 60:02d}:{int(minute) % 60:02d}"


def paired_rows(frame):
    return frame.loc[(frame["status"] == "resolved")
                     & np.isfinite(frame["error_seconds"])
                     & np.isfinite(frame["transloc_error_seconds"])].copy()


def long_errors(frame):
    return frame.melt(id_vars=["Route", "Destination", "horizon_stops", "Time of day"],
                      value_vars=["error_seconds", "transloc_error_seconds"],
                      var_name="Predictor", value_name="Signed error (seconds)").assign(
        Predictor=lambda x: x["Predictor"].map(
            {"error_seconds": "Your model", "transloc_error_seconds": "TransLoc"}),
        **{"Absolute error (seconds)": lambda x: x["Signed error (seconds)"].abs()})


def comparison_table(frame, group):
    records = []
    for label, part in frame.groupby(group, observed=True, sort=True):
        ours, vendor = part["error_seconds"], part["transloc_error_seconds"]
        records.append({group: label, "Forecasts": len(part),
                        "Distinct arrivals": len(part.drop_duplicates(
                            ["vehicle_id", "destination_route_stop_id", "actual_arrival"])),
                        "Your MAE": ours.abs().mean(), "TransLoc MAE": vendor.abs().mean(),
                        "MAE gain": vendor.abs().mean() - ours.abs().mean(),
                        "Your bias": ours.mean(), "TransLoc bias": vendor.mean(),
                        "Your P90": ours.abs().quantile(.9),
                        "TransLoc P90": vendor.abs().quantile(.9)})
    return pd.DataFrame(records)


def chart(figure):
    figure.update_layout(margin=dict(l=10, r=10, t=40, b=10), legend_title_text="")
    st.plotly_chart(figure, width="stretch")


def render(path):
    try:
        frame = load_results(str(path), path.stat().st_mtime_ns)
    except (OSError, ValueError, KeyError) as error:
        st.error(f"Could not read this run's results: {error}")
        return
    if frame.empty:
        st.info("No finalized forecasts yet. Leave collection running and refresh.")
        return
    st.caption(f"Run: {path.parent.name} · Latest finalized outcome: "
               f"{frame['finished_at'].max().tz_convert('America/New_York'):%b %d, %I:%M:%S %p %Z}")
    a, b, c = st.columns(3)
    variants = sorted(frame["bucket_minutes"].dropna().astype(int).unique())
    width = a.selectbox("Model averaging interval", variants,
                        format_func=lambda x: "No time buckets (baseline)" if x == 0 else f"{x} minutes")
    routes = b.multiselect("Routes (empty = all)", sorted(frame["Route"].unique()))
    dates = c.multiselect("Departure dates (empty = all)", sorted(frame["Departure date"].dropna().unique()))
    filtered = frame.loc[frame["bucket_minutes"] == width].copy()
    if routes:
        filtered = filtered[filtered["Route"].isin(routes)]
    if dates:
        filtered = filtered[filtered["Departure date"].isin(dates)]
    a, b, c = st.columns(3)
    horizons = a.multiselect("Stops ahead (empty = all)", sorted(filtered["horizon_stops"].dropna().astype(int).unique()))
    origins = b.multiselect("Origin stops (empty = all)", sorted(filtered["Origin"].unique()))
    destinations = c.multiselect("Destination stops (empty = all)", sorted(filtered["Destination"].unique()))
    if horizons:
        filtered = filtered[filtered["horizon_stops"].isin(horizons)]
    if origins:
        filtered = filtered[filtered["Origin"].isin(origins)]
    if destinations:
        filtered = filtered[filtered["Destination"].isin(destinations)]
    a, b, c = st.columns(3)
    time_width = a.selectbox("Group departure time into", [30, 60, 90, 120, 150, 180, 210, 240], index=1,
                             format_func=lambda x: f"{x}-minute intervals")
    start = filtered["departure_minute"] // time_width * time_width
    filtered["Time of day"] = start.map(lambda x: f"{clock_label(x)}–{clock_label(x + time_width)}")
    times = b.multiselect("Departure intervals (empty = all)", sorted(filtered["Time of day"].unique()))
    quality = c.selectbox("Intermediate arrival observations", ["Include all", "Complete only", "Missing intermediates only"])
    if times:
        filtered = filtered[filtered["Time of day"].isin(times)]
    if quality != "Include all":
        flag = "0" if quality == "Complete only" else "1"
        filtered = filtered[filtered["intermediate_observations_missing"] == flag]

    paired = paired_rows(filtered)
    if width and not paired.empty:
        fallback_count = (paired["fallback_used"] == "1").sum()
        st.caption(f"Selected model: {fallback_count:,} of {len(paired):,} paired forecasts "
                   "used the no-bucket fallback because the time bucket lacked historical support.")
    st.caption("All times of day are Eastern, based on departure from the origin. "
               "One model variant at a time; both predictors use the same scored forecasts. "
               "Stops identify route-specific stop occurrences, including direction.")
    counts = filtered["status"].value_counts()
    a, b, c, d = st.columns(4)
    a.metric("Paired forecasts", f"{len(paired):,}")
    b.metric("Resolved", f"{counts.get('resolved', 0):,}")
    c.metric("Excluded", f"{counts.get('excluded', 0):,}")
    d.metric("Unavailable", f"{counts.get('unavailable', 0):,}")
    st.caption("Counts cover finalized outcomes matching your filters. Pending forecasts are not in this export. "
               "Forecasts can share an actual arrival and are not independent bus trips.")
    distributions, breakdown, individual, exclusions = st.tabs(
        ["Error distributions", "Compare groups", "Individual forecasts", "Exclusions"])
    with exclusions:
        excluded = filtered[filtered["status"] == "excluded"]
        if excluded.empty:
            st.info("No exclusions match these filters.")
        else:
            reasons = excluded.groupby(["Route", "reason"]).size().reset_index(name="Forecasts")
            chart(px.bar(reasons, x="reason", y="Forecasts", color="Route", barmode="stack"))
            st.dataframe(reasons, hide_index=True, width="stretch")
        st.caption("Exclusions have no reliable arrival label and are omitted from accuracy distributions. "
                   "The intermediate-observation filter also applies here; excluded rows may not yet have that flag.")
    if paired.empty:
        with distributions:
            st.info("No paired resolved forecasts match these filters. Broaden the selection or wait for more arrivals.")
        return
    errors = long_errors(paired)
    ours, vendor = paired["error_seconds"], paired["transloc_error_seconds"]
    with distributions:
        a, b, c, d = st.columns(4)
        a.metric("Your MAE", f"{ours.abs().mean():.1f} sec")
        b.metric("TransLoc MAE", f"{vendor.abs().mean():.1f} sec")
        c.metric("MAE gain (positive = yours better)", f"{vendor.abs().mean() - ours.abs().mean():+.1f} sec")
        d.metric("Distinct observed arrivals", len(paired.drop_duplicates(
            ["vehicle_id", "destination_route_stop_id", "actual_arrival"])))
        st.caption("Error = predicted arrival − observed arrival. Negative is early; positive is late. "
                   "Zero is the observed arrival. Labels come from the GPS detector, not verified door-opening times.")
        bins = st.slider("Histogram bins", 10, 150, 50, step=10)
        histogram = px.histogram(errors, x="Signed error (seconds)", color="Predictor",
                                 nbins=bins, barmode="overlay", opacity=.55,
                                 color_discrete_map=COLORS, histnorm="probability",
                                 title="How early or late were the predictions?")
        histogram.add_vline(x=0, line_dash="dash", line_color="#64748b")
        chart(histogram)
        chart(px.ecdf(errors, x="Absolute error (seconds)", color="Predictor",
                      color_discrete_map=COLORS, title="Fraction of predictions within each error threshold"))
        summary = pd.DataFrame({"Predictor": ["Your model", "TransLoc"],
            "MAE (sec)": [ours.abs().mean(), vendor.abs().mean()],
            "Median absolute (sec)": [ours.abs().median(), vendor.abs().median()],
            "P90 absolute (sec)": [ours.abs().quantile(.9), vendor.abs().quantile(.9)],
            "Bias (sec)": [ours.mean(), vendor.mean()],
            "Early (%)": [(ours < 0).mean() * 100, (vendor < 0).mean() * 100],
            "Within 60 sec (%)": [(ours.abs() <= 60).mean() * 100, (vendor.abs() <= 60).mean() * 100]})
        st.dataframe(summary.round(1), hide_index=True, width="stretch")
    with breakdown:
        group = st.selectbox("Compare distributions by", ["Route", "Destination", "Origin", "horizon_stops", "Time of day"])
        # Origin is only needed for this view, avoiding duplicated wide data elsewhere.
        group_errors = paired.melt(id_vars=[group], value_vars=["error_seconds", "transloc_error_seconds"],
                                   var_name="Predictor", value_name="Signed error (seconds)")
        group_errors["Predictor"] = group_errors["Predictor"].map(
            {"error_seconds": "Your model", "transloc_error_seconds": "TransLoc"})
        figure = px.box(group_errors, x=group, y="Signed error (seconds)", color="Predictor",
                        points=False, color_discrete_map=COLORS)
        figure.add_hline(y=0, line_dash="dash", line_color="#64748b")
        chart(figure)
        st.dataframe(comparison_table(paired, group).round(1), hide_index=True, width="stretch")
        st.caption("Boxes show the median and middle 50%; whiskers summarize spread. "
                   "Outlier markers are hidden here; every error remains in the distributions and metrics.")
    with individual:
        st.caption("Elapsed time is measured from the observed origin arrival, so model, TransLoc, and actuals share a clock.")
        elapsed = pd.DataFrame({"Actual elapsed (sec)": (paired["actual_arrival"] - paired["origin_arrival"]).dt.total_seconds(),
                                "Your model": (paired["predicted_arrival"] - paired["origin_arrival"]).dt.total_seconds(),
                                "TransLoc": (paired["transloc_predicted_arrival"] - paired["origin_arrival"]).dt.total_seconds(),
                                "Route": paired["Route"], "Destination": paired["Destination"],
                                "Stops ahead": paired["horizon_stops"]})
        scatter_data = elapsed.melt(id_vars=["Actual elapsed (sec)", "Route", "Destination", "Stops ahead"],
                                    value_vars=["Your model", "TransLoc"], var_name="Predictor", value_name="Predicted elapsed (sec)")
        figure = px.scatter(scatter_data, x="Actual elapsed (sec)", y="Predicted elapsed (sec)",
                            color="Predictor", opacity=.45, color_discrete_map=COLORS,
                            hover_data=["Route", "Destination", "Stops ahead"])
        high = max(scatter_data["Actual elapsed (sec)"].max(), scatter_data["Predicted elapsed (sec)"].max())
        figure.add_shape(type="line", x0=0, y0=0, x1=high, y1=high, line=dict(dash="dash", color="#64748b"))
        chart(figure)
        ordered = paired.sort_values("origin_arrival", ascending=False).reset_index(drop=True)
        selected = st.selectbox("Inspect a forecast", range(len(ordered)), format_func=lambda i:
            f"{ordered.iloc[i]['origin_arrival'].tz_convert('America/New_York'):%m/%d %H:%M:%S} · "
            f"{ordered.iloc[i]['Route']} · bus {ordered.iloc[i]['vehicle_id']} · "
            f"{ordered.iloc[i]['Destination']} · {int(ordered.iloc[i]['horizon_stops'])} stops ahead")
        row = ordered.iloc[selected]
        figure = go.Figure()
        for name, column, color in [("Your model", "predicted_arrival", COLORS["Your model"]),
                                    ("TransLoc", "transloc_predicted_arrival", COLORS["TransLoc"]),
                                    ("Observed arrival", "actual_arrival", "#16a34a")]:
            figure.add_trace(go.Scatter(x=[row[column].tz_convert("America/New_York").isoformat()],
                                       y=[name], name=name, mode="markers", marker=dict(size=15, color=color)))
        figure.update_xaxes(type="date", title="Arrival time (Eastern, offset shown on hover)")
        chart(figure)
        columns = ["prediction_id", "Route", "vehicle_id", "Origin", "Destination", "horizon_stops",
                   "origin_arrival", "predicted_arrival", "transloc_predicted_arrival", "actual_arrival",
                   "error_seconds", "transloc_error_seconds", "sample_count", "fallback_used",
                   "intermediate_observations_missing", "missed_intermediate_count"]
        display = ordered[columns].copy()
        for column in ["origin_arrival", "predicted_arrival", "transloc_predicted_arrival", "actual_arrival"]:
            display[column] = display[column].dt.tz_convert("America/New_York").astype(str)
        st.dataframe(display, hide_index=True, width="stretch")
        st.download_button("Download filtered paired forecasts", display.to_csv(index=False),
                           "paired_forecasts.csv", "text/csv")


def main():
    st.set_page_config(page_title="Bus ETA evaluation", page_icon="🚌", layout="wide")
    st.title("Your ETAs vs. TransLoc")
    st.write("Explore prediction accuracy against observed arrivals, from whole routes down to a single forecast.")
    runs = sorted(RUNS.glob("*/eval_results.csv"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    if not runs:
        st.info("No evaluation results found. Start the evaluator first.")
        return
    with st.sidebar:
        source = st.selectbox("Evaluation run", runs, format_func=lambda p: p.parent.name)
        refresh = st.toggle("Refresh every 30 seconds", value=True)
        st.button("Refresh now")
        st.caption("Reads existing results only. Start or stop collection separately. "
                   "Runs stay separate because detector versions and observation windows can differ.")
    st.fragment(run_every=30 if refresh else None)(render)(source)


if __name__ == "__main__":
    main()
