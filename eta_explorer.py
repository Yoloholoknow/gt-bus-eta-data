"""Local Streamlit explorer for observed stop timing and ETA horizons."""

from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st


ROOT = Path(__file__).resolve().parent
VISITS = ROOT / "data" / "stops_timings" / "combined_stop_visits.csv"
ROUTES = ROOT / "reference" / "routes.json"

st.set_page_config(page_title="GT Bus ETA Explorer", page_icon="🚌", layout="wide")


@st.cache_data
def load_data(path: str, routes_path: str):
    data = pd.read_csv(path)
    data["route_color"] = data["route_color"].fillna("Unknown route").astype(str)
    data["stop_name"] = data["stop_name"].fillna("Unknown stop").astype(str)
    for col in ("route_id", "route_stop_id", "vehicle_number"):
        data[col] = data[col].astype("string")
    for col in ("is_arriving_time", "at_stop_time", "is_departing_time"):
        # Source timestamps include explicit UTC offsets; keep and respect them.
        data[col] = pd.to_datetime(data[col], errors="coerce", utc=True).dt.tz_convert("America/New_York")
    data["dwell_seconds"] = pd.to_numeric(data["time_at_stop_seconds"], errors="coerce")

    route_data = pd.read_json(routes_path)
    route_meta, sequence = {}, {}
    for route in route_data.to_dict("records"):
        name = str(route.get("Description") or "Unknown route")
        route_meta[name] = {"color": route.get("MapLineColor", "#55728a"), "id": str(route.get("RouteID", ""))}
        sequence[name] = [str(stop.get("RouteStopID")) for stop in route.get("Stops", [])]
    # Use IDs internally to disambiguate repeated/renamed stops, never in rendered labels.
    data["_route_stop"] = data["route_stop_id"].astype(str)
    data["_route_id"] = data["route_id"].astype(str)
    data["_visit_order"] = pd.to_datetime(data["at_stop_time"], errors="coerce", utc=True)
    route_id_to_name = {meta["id"]: name for name, meta in route_meta.items()}
    data["_route_name"] = data["_route_id"].map(route_id_to_name).fillna(data["route_color"])
    data["_route_seq"] = data.apply(
        lambda r: sequence.get(r["_route_name"], []).index(r["_route_stop"])
        if r["_route_stop"] in sequence.get(r["_route_name"], []) else -1,
        axis=1,
    )
    return data, route_meta, sequence


def clock_bin(ts, bin_minutes):
    minutes_after_midnight = ts.dt.hour * 60 + ts.dt.minute
    return (minutes_after_midnight // bin_minutes).astype(int)


def bin_label(value, bin_minutes):
    total_minutes = int(value) * bin_minutes
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def time_label_order(bin_minutes):
    return [bin_label(value, bin_minutes) for value in range(1440 // bin_minutes)]


def summary(frame, value, dims):
    return frame.groupby(dims, observed=True)[value].agg(
        median="median", p90=lambda s: s.quantile(.9), samples="count"
    ).reset_index()


def remove_outliers(frame, value, groups, minimum=8):
    """Drop Tukey 1.5×IQR outliers within comparable route/stop/time groups."""
    if frame.empty:
        return frame.copy(), 0
    grouped = frame.groupby(groups, dropna=False)[value]
    q1 = grouped.transform(lambda values: values.quantile(.25))
    q3 = grouped.transform(lambda values: values.quantile(.75))
    count = grouped.transform("count")
    spread = q3 - q1
    keep = (count < minimum) | frame[value].between(q1 - 1.5 * spread, q3 + 1.5 * spread)
    clean = frame.loc[keep].copy()
    return clean, len(frame) - len(clean)


def add_stats(frame, metric, min_samples):
    med = frame["median"].map(lambda x: f"{x:.0f}s")
    p90 = frame["p90"].map(lambda x: f"{x:.0f}s")
    low_samples = frame["samples"] < min_samples
    high_variability = (frame["p90"] > frame["median"] * 1.5) | ((frame["median"] == 0) & (frame["p90"] > 0))
    note = pd.Series("", index=frame.index)
    note.loc[low_samples] = " · SPARSE"
    note.loc[low_samples & high_variability] = " · UNSURE"
    return frame.assign(summary=med + " median · " + p90 + " p90 · n=" + frame["samples"].astype(str) + note)


def apply_select_all(names):
    selected = st.session_state["select_all_stops"]
    for index, _ in enumerate(names):
        st.session_state[f"stop_{index}"] = selected


def update_select_all(names):
    st.session_state["select_all_stops"] = all(
        st.session_state.get(f"stop_{index}", False) for index, _ in enumerate(names)
    )


data, route_meta, route_sequences = load_data(str(VISITS), str(ROUTES))
st.title("GT Bus ETA Explorer")
st.caption("Observed stop visits · local time (America/New_York) · robust summaries use median and 90th percentile")

route_names = sorted(data["_route_name"].dropna().unique())
route_palette = {name: route_meta.get(name, {}).get("color", "#55728a") for name in route_names}
with st.sidebar:
    st.header("Explore")
    selected_route = st.selectbox("Route", route_names)
    horizon = st.select_slider("Multi-stop horizon", options=[2, 3, 4, 5], value=3, format_func=lambda n: f"{n} stops ahead")
    bin_minutes = st.select_slider("Time bin size", options=[30, 60, 90, 120, 150, 180, 210, 240], value=30,
                                   format_func=lambda n: f"{n // 60} hr" if n % 60 == 0 and n >= 60 else f"{n} min")
    min_samples = st.number_input("Mark bins sparse below samples", 1, 100, 10)
    st.markdown(f"<span style='display:inline-block;width:12px;height:12px;background:{route_palette[selected_route]};border-radius:50%;margin-right:7px'></span>{selected_route}", unsafe_allow_html=True)

base = data[data["_route_name"] == selected_route].copy()
base = base.dropna(subset=["at_stop_time"])
base["time_bin"] = clock_bin(base["at_stop_time"], bin_minutes)
stop_names = sorted(base["stop_name"].dropna().unique())
stop_colors = px.colors.qualitative.Dark24 + px.colors.qualitative.Light24
stop_palette = {name: stop_colors[index % len(stop_colors)] for index, name in enumerate(stop_names)}
with st.sidebar:
    st.markdown("**Stops to display**")
    st.checkbox("Select all stops", value=True, key="select_all_stops", on_change=apply_select_all, args=(stop_names,))
    selected_stops = []
    for index, stop_name in enumerate(stop_names):
        if st.checkbox(stop_name, value=True, key=f"stop_{index}", on_change=update_select_all, args=(stop_names,)):
            selected_stops.append(stop_name)
all_route_visits = base.copy()
base = base[base["stop_name"].isin(selected_stops)].copy()

st.info(
    "Filters and assumptions: visits with missing timestamps are omitted; dwell uses the recorded geofence duration. "
    "Travel is departure-to-next-stop arrival; zero or negative intervals are excluded. Multi-stop elapsed time runs "
    "from origin arrival to destination arrival and therefore includes dwell at intermediate stops. Route stop order "
    f"comes from the saved reference data. Bins use {bin_minutes}-minute local-time windows. Outliers are removed within each "
    "route, stop pair/horizon, and time bin using Tukey 1.5×IQR fences when a group has at least 8 observations; "
    "smaller groups are left intact and flagged sparse. Summaries use median and p90 of retained observations. "
    f"Bins with fewer than {min_samples} retained observations are marked sparse; if p90 is also greater than "
    "1.5× median, they are marked UNSURE due to low coverage and high variability."
)

st.subheader("Dwell at each stop")
dwell = base.dropna(subset=["dwell_seconds"])
dwell = dwell[(dwell["dwell_seconds"] >= 0) & (dwell["dwell_seconds"] < 3600)]
dwell, dwell_removed = remove_outliers(dwell, "dwell_seconds", ["stop_name", "time_bin", "route_color"])
dwell_stats = add_stats(summary(dwell, "dwell_seconds", ["stop_name", "time_bin", "route_color"]), "dwell_seconds", min_samples)
if len(dwell_stats):
    dwell_stats = dwell_stats.sort_values(["stop_name", "time_bin"])
    dwell_stats["time"] = dwell_stats["time_bin"].map(lambda value: bin_label(value, bin_minutes))
    fig = px.line(dwell_stats, x="time", y="median", color="stop_name", line_group="stop_name",
                  color_discrete_map=stop_palette, markers=True, hover_data={"p90": True, "samples": True, "summary": True},
                  category_orders={"time": time_label_order(bin_minutes)}, title="Median dwell time by local time of day",
                  labels={"median": "Median seconds", "time": "Local time"})
    fig.update_layout(height=440)
    st.plotly_chart(fig, width="stretch")
    st.caption(f"Stop names are shown in the legend; hover for p90 and sample count. Values above one hour are excluded; {dwell_removed:,} additional IQR outliers removed.")
else:
    st.warning("No dwell observations match the selected routes.")


def make_pairs(visits, sequences):
    pairs, horizons = [], []
    for route, group in visits.groupby("_route_name", sort=False):
        ordered_stops = sequences.get(route, [])
        if not ordered_stops:
            continue
        for (_, vehicle), bus in group.groupby(["_route_id", "vehicle_number"], dropna=False, sort=False):
            bus = bus.sort_values("at_stop_time")
            rows = bus.to_dict("records")
            # Pair visits by route order, allowing a skipped observation but never reverse travel.
            for i, origin in enumerate(rows):
                origin_idx = origin["_route_seq"]
                if origin_idx < 0 or pd.isna(origin["is_departing_time"]):
                    continue
                for hop in range(1, 6):
                    target_index = origin_idx + hop
                    candidate = next((j for j in range(i + 1, len(rows)) if rows[j]["_route_seq"] == target_index), None)
                    if candidate is None:
                        continue
                    # Require every intervening named stop visit to be observed in order.
                    cursor = i
                    complete = True
                    for expected in range(origin_idx + 1, target_index + 1):
                        found = next((j for j in range(cursor + 1, candidate + 1)
                                      if rows[j]["_route_seq"] == expected), None)
                        if found is None:
                            complete = False
                            break
                        cursor = found
                    if not complete:
                        continue
                    dest = rows[candidate]
                    if hop == 1 and pd.notna(origin["is_departing_time"]):
                        travel = (dest["at_stop_time"] - origin["is_departing_time"]).total_seconds()
                        if travel > 0:
                            pairs.append({"route": route, "route_color": route, "origin": origin["stop_name"], "destination": dest["stop_name"],
                                          "time_bin": int(clock_bin(pd.Series([origin["is_departing_time"]]), bin_minutes).iloc[0]), "seconds": travel})
                    elapsed = (dest["at_stop_time"] - origin["at_stop_time"]).total_seconds()
                    if hop >= 2 and elapsed > 0:
                        horizons.append({"route": route, "route_color": route, "origin": origin["stop_name"], "destination": dest["stop_name"],
                                         "hops": hop, "time_bin": int(clock_bin(pd.Series([origin["at_stop_time"]]), bin_minutes).iloc[0]), "seconds": elapsed})
    return pd.DataFrame(pairs), pd.DataFrame(horizons)


pairs, horizons = make_pairs(all_route_visits, route_sequences)
if len(pairs):
    pairs = pairs[pairs["origin"].isin(selected_stops)]
if len(horizons):
    horizons = horizons[horizons["origin"].isin(selected_stops)]
st.subheader("Travel between consecutive named stops")
if len(pairs):
    pairs, pairs_removed = remove_outliers(pairs, "seconds", ["origin", "destination", "time_bin", "route"])
    ps = add_stats(summary(pairs, "seconds", ["origin", "destination", "time_bin", "route"]), "seconds", min_samples)
    ps = ps.sort_values(["origin", "destination", "time_bin"])
    ps["time"] = ps["time_bin"].map(lambda value: bin_label(value, bin_minutes))
    ps["segment"] = ps["origin"] + " → " + ps["destination"]
    ps["trace"] = ps["segment"]
    st.plotly_chart(px.line(ps, x="time", y="median", color="origin", line_group="trace", markers=True,
        color_discrete_map=stop_palette, hover_data={"origin": True, "destination": True, "p90": True, "samples": True, "summary": True},
        category_orders={"time": time_label_order(bin_minutes)}, labels={"median": "Median seconds", "time": "Local time"},
        title="Median travel time · departure to next stop"), width="stretch")
    st.caption(f"Line colors identify origin stops; hover for the destination, p90, and sample count. Only positive departure-to-arrival intervals are included; {pairs_removed:,} IQR outliers removed.")
else:
    st.warning("No valid consecutive-stop travel intervals are available for these routes.")

st.subheader(f"Cumulative time · {horizon} stops ahead")
if len(horizons):
    hs = horizons[horizons["hops"] == horizon]
    if len(hs):
        hs, horizons_removed = remove_outliers(hs, "seconds", ["origin", "destination", "time_bin", "route", "hops"])
        hs = add_stats(summary(hs, "seconds", ["origin", "destination", "time_bin", "route"]), "seconds", min_samples)
        hs = hs.sort_values(["origin", "destination", "time_bin"])
        hs["time"] = hs["time_bin"].map(lambda value: bin_label(value, bin_minutes))
        hs["segment"] = hs["origin"] + " → " + hs["destination"]
        hs["trace"] = hs["segment"]
        st.plotly_chart(px.line(hs, x="time", y="median", color="origin", line_group="trace", markers=True,
            color_discrete_map=stop_palette, hover_data={"origin": True, "destination": True, "p90": True, "samples": True, "summary": True},
            category_orders={"time": time_label_order(bin_minutes)}, labels={"median": "Median elapsed seconds", "time": "Origin local time"},
            title="Origin arrival to destination arrival"), width="stretch")
        st.caption(f"Line colors identify origin stops; hover for the destination, p90, and sample count. Intermediate dwell is included; dwell at the origin is excluded. {horizons_removed:,} IQR outliers removed.")
    else:
        st.warning(f"No valid {horizon}-stop observations are available for these routes.")
else:
    st.warning("No multi-stop observations could be assembled. Check that visits align with the saved route stop order.")

st.subheader("Data coverage")
if len(horizons):
    coverage = horizons.groupby(["route", "hops"], as_index=False).agg(samples=("seconds", "size"),
        origin_stops=("origin", "nunique"), destination_stops=("destination", "nunique"))
    st.dataframe(coverage.rename(columns={"route": "Route", "hops": "Stops ahead", "samples": "Valid observations",
        "origin_stops": "Origin stops", "destination_stops": "Destination stops"}), hide_index=True, width="stretch")
st.caption("Coverage interpretation: hover shows sample count. Low-sample bins are marked SPARSE; when p90 exceeds 1.5× median, they are marked UNSURE to indicate high variability with limited coverage.")

st.subheader("GPS data and later map matching")
gps_dir = ROOT / "data" / "vehicle_heading"
gps_files = sorted(gps_dir.glob("vehicle_points_*.csv")) if gps_dir.exists() else []
st.write(f"GPS collector files found: **{len(gps_files)}**. They are not used in these summaries." if not gps_files else
         f"GPS collector files found: **{len(gps_files)}**. They are inspected as an available refinement source, but are not used in these summaries.")
st.markdown("A later refinement can join GPS fixes to the route's saved stop coordinates, assign each fix to the nearest plausible stop within a distance threshold, and detect geofence entry/exit per vehicle and route. Timestamp alignment and route direction/order can reject wrong-side or out-of-sequence matches. GPS-derived arrivals can then replace the current visit boundaries and be compared against these baseline distributions. The existing heading collector saves timestamped latitude/longitude and route observations when its CSV files are present.")
