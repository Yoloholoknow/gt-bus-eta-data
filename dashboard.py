"""Local Streamlit dashboard for vehicle heading CSV files.

Run from the repository root with:

    streamlit run dashboard.py
"""

import json
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "vehicle_heading"
ROUTES_PATH = ROOT / "reference" / "routes.json"
ROUTES_URL = "https://bus.gatech.edu/Services/JSONPRelay.svc/GetRoutesForMapWithScheduleWithEncodedLine"
EXPECTED_COLUMNS = [
    "tick",
    "fetched_at",
    "source_timestamp",
    "seconds",
    "vehicle_id",
    "vehicle_name",
    "route_id",
    "latitude",
    "longitude",
    "ground_speed",
    "heading",
]


@st.cache_data(ttl=5)
def load_points(data_dir: str) -> pd.DataFrame:
    paths = sorted(Path(data_dir).glob("vehicle_points_*.csv"))
    if not paths:
        return pd.DataFrame(columns=EXPECTED_COLUMNS)

    frames = [pd.read_csv(path) for path in paths if path.stat().st_size > 0]
    if not frames:
        return pd.DataFrame(columns=EXPECTED_COLUMNS)

    data = pd.concat(frames, ignore_index=True)
    for column in ("seconds", "latitude", "longitude", "ground_speed", "heading"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["fetched_at"] = pd.to_datetime(data["fetched_at"], errors="coerce", utc=True)
    data["vehicle_id"] = data["vehicle_id"].astype("string")
    data["vehicle_name"] = data["vehicle_name"].fillna("").astype(str)
    data["route_id"] = data["route_id"].astype("string")
    data = data.dropna(subset=["fetched_at"])
    data["cardinal_direction"] = data["heading"].map(cardinal_direction)
    return data.sort_values("fetched_at")


def cardinal_direction(value: float | int | None) -> str:
    if pd.isna(value):
        return "Unknown"
    labels = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return labels[int(((float(value) % 360) + 22.5) // 45) % 8]


@st.cache_data(ttl=3600)
def load_routes(reference_path: str) -> list[dict]:
    """Load route geometry from reference data, falling back to the live API."""
    path = Path(reference_path)
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open(encoding="utf-8") as source:
                payload = json.load(source)
            if isinstance(payload, list):
                return [route for route in payload if isinstance(route, dict)]
        except (OSError, json.JSONDecodeError):
            pass

    try:
        response = requests.get(ROUTES_URL, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return [route for route in payload if isinstance(route, dict)]
    except (requests.RequestException, ValueError):
        pass
    return []


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decode a Google encoded polyline into (latitude, longitude) pairs."""
    coordinates: list[tuple[float, float]] = []
    index = 0
    latitude = 0
    longitude = 0

    try:
        while index < len(encoded):
            value = 0
            shift = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                value |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            latitude += ~(value >> 1) if value & 1 else value >> 1

            value = 0
            shift = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                value |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            longitude += ~(value >> 1) if value & 1 else value >> 1
            coordinates.append((latitude / 100000, longitude / 100000))
    except (IndexError, TypeError):
        return []

    return coordinates


def route_color(route_id: str, routes_by_id: dict[str, dict]) -> str:
    route = routes_by_id.get(str(route_id), {})
    configured = route.get("MapLineColor")
    if configured:
        return str(configured)

    palette = px.colors.qualitative.Set2
    numeric_id = sum(ord(character) for character in str(route_id))
    return palette[numeric_id % len(palette)]


def add_route_lines_map(
    figure: go.Figure,
    route_ids: list[str],
    routes_by_id: dict[str, dict],
) -> int:
    line_count = 0
    for route_id in route_ids:
        route = routes_by_id.get(str(route_id), {})
        points = decode_polyline(route.get("EncodedPolyline", ""))
        if len(points) < 2:
            continue

        figure.add_trace(
            go.Scattermap(
                lat=[point[0] for point in points],
                lon=[point[1] for point in points],
                mode="lines",
                name=f"Route {route_id} — {route.get('Description', '')}".strip(" —"),
                legendgroup=f"route-{route_id}",
                line={"color": route_color(route_id, routes_by_id), "width": 7},
                opacity=0.38,
                hoverinfo="skip",
            )
        )
        line_count += 1
    return line_count


def add_bus_markers_map(
    figure: go.Figure,
    latest: pd.DataFrame,
    routes_by_id: dict[str, dict],
) -> None:
    for _, row in latest.sort_values(["route_id", "vehicle_id"]).iterrows():
        vehicle_id = str(row["vehicle_id"])
        vehicle_name = str(row["vehicle_name"]).strip() or vehicle_id
        route_id = str(row["route_id"])
        heading = float(row["heading"]) if pd.notna(row["heading"]) else 0.0
        speed = row["ground_speed"]
        source_age = row["seconds"]
        direction = row["cardinal_direction"]
        bus_label = f"Bus {vehicle_name} — Route {route_id}"
        hover = (
            f"<b>{bus_label}</b><br>"
            f"Vehicle ID: {vehicle_id}<br>"
            f"Heading: {heading:.0f}° ({direction})<br>"
            f"Ground speed: {speed:.2f}<br>"
            f"Source age: {source_age:.1f}s<extra></extra>"
            if pd.notna(speed) and pd.notna(source_age)
            else f"<b>{bus_label}</b><br>Vehicle ID: {vehicle_id}<br>Heading: {heading:.0f}° ({direction})<extra></extra>"
        )
        color = route_color(route_id, routes_by_id)

        # A white halo makes buses visible over both route lines and map tiles.
        figure.add_trace(
            go.Scattermap(
                lat=[row["latitude"]],
                lon=[row["longitude"]],
                mode="markers",
                marker={"symbol": "circle", "size": 31, "color": "white", "allowoverlap": True},
                hoverinfo="skip",
                showlegend=False,
            )
        )
        # MapLibre rotates this arrow clockwise from true north using Heading.
        figure.add_trace(
            go.Scattermap(
                lat=[row["latitude"]],
                lon=[row["longitude"]],
                mode="markers",
                name=bus_label,
                legendgroup=f"route-{route_id}",
                marker={
                    "symbol": "arrow",
                    "size": 27,
                    "color": color,
                    "angle": [heading],
                    "allowoverlap": True,
                },
                hovertemplate=hover,
                showlegend=True,
            )
        )


def filter_data(data: pd.DataFrame) -> pd.DataFrame:
    st.sidebar.header("Filters")
    filtered = data

    dates = data["fetched_at"].dt.date
    date_range = st.sidebar.date_input(
        "Date range",
        value=(dates.min(), dates.max()),
        min_value=dates.min(),
        max_value=dates.max(),
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start, end = date_range
        filtered = filtered[(dates >= start) & (dates <= end)]

    routes = sorted(filtered["route_id"].dropna().unique().tolist())
    selected_routes = st.sidebar.multiselect("Routes", routes, default=routes)
    if selected_routes:
        filtered = filtered[filtered["route_id"].isin(selected_routes)]

    vehicles = sorted(filtered["vehicle_id"].dropna().unique().tolist())
    selected_vehicles = st.sidebar.multiselect("Vehicles", vehicles)
    if selected_vehicles:
        filtered = filtered[filtered["vehicle_id"].isin(selected_vehicles)]

    return filtered


def polling_stats(data: pd.DataFrame) -> tuple[float | None, int, float | None]:
    poll_times = data["fetched_at"].drop_duplicates().sort_values()
    gaps = poll_times.diff().dt.total_seconds().dropna()
    if gaps.empty:
        return None, 0, None
    return float(gaps.median()), int((gaps > 7.5).sum()), float(gaps.max())


def info_header(title: str, explanation: str) -> None:
    """Render a section title with a clickable explanation popover."""
    title_column, info_column = st.columns([0.94, 0.06])
    with title_column:
        st.subheader(title)
    with info_column:
        with st.popover("ⓘ"):
            st.markdown(f"**{title}**")
            st.write(explanation)


def timeline_control(data: pd.DataFrame) -> pd.Timestamp:
    """Select a recorded poll time; moving the slider reruns the dashboard."""
    poll_times = data["fetched_at"].drop_duplicates().sort_values()
    if len(poll_times) == 1:
        selected_time = poll_times.iloc[0]
        st.caption(f"Showing the only recorded poll: {selected_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        return selected_time

    info_header(
        "Playback timeline",
        "Move the slider to replay the recorded polls. The map updates to the nearest available poll and "
        "the arrows show the heading of every bus observed at that moment. This is a replay of collected data, "
        "not a prediction of future bus locations.",
    )
    selected_value = st.slider(
        "Recorded poll time",
        min_value=poll_times.iloc[0].to_pydatetime(),
        max_value=poll_times.iloc[-1].to_pydatetime(),
        value=poll_times.iloc[-1].to_pydatetime(),
        step=timedelta(seconds=1),
        format="MM/DD/YY HH:mm:ss",
        help="Drag the timeline. The dashboard selects the nearest recorded poll.",
    )
    selected_timestamp = pd.Timestamp(selected_value)
    if selected_timestamp.tzinfo is None:
        selected_timestamp = selected_timestamp.tz_localize("UTC")
    else:
        selected_timestamp = selected_timestamp.tz_convert("UTC")
    nearest_index = (poll_times - selected_timestamp).abs().argmin()
    selected_time = poll_times.iloc[nearest_index]
    st.caption(
        f"Showing recorded poll: {selected_time.strftime('%Y-%m-%d %H:%M:%S UTC')} "
        f"({len(poll_times):,} available polls)"
    )
    return selected_time


def render_map(data: pd.DataFrame) -> None:
    info_header(
        "Latest bus positions",
        "Shows the newest recorded position for each bus. Colored route lines show the route geometry; "
        "the large triangle points in the API Heading direction. The legend identifies both routes and buses. "
        "This is compass heading, not necessarily forward/reverse route direction on a curved route.",
    )
    selected_time = timeline_control(data)
    latest_time = selected_time
    latest = data[data["fetched_at"] == latest_time].dropna(subset=["latitude", "longitude"])
    if latest.empty:
        st.info("No valid coordinates are available for the selected data.")
        return

    map_view = st.radio(
        "Map view",
        ["Offline coordinate view", "OpenStreetMap street view"],
        horizontal=True,
        help="The street view requires your browser to load OpenStreetMap tiles. The offline view always works.",
    )
    title = f"Positions at {latest_time.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    routes = load_routes(str(ROUTES_PATH))
    routes_by_id = {
        str(route.get("RouteID")): route
        for route in routes
        if route.get("RouteID") is not None
    }
    route_ids = sorted(latest["route_id"].dropna().astype(str).unique().tolist())

    hover_data = {
        "vehicle_id": True,
        "heading": True,
        "cardinal_direction": True,
        "ground_speed": True,
        "seconds": True,
        "latitude": False,
        "longitude": False,
    }

    if map_view == "OpenStreetMap street view":
        figure = go.Figure()
        line_count = add_route_lines_map(figure, route_ids, routes_by_id)
        add_bus_markers_map(figure, latest, routes_by_id)
        figure.update_layout(
            map={
                "style": "open-street-map",
                "center": {
                    "lat": float(latest["latitude"].mean()),
                    "lon": float(latest["longitude"].mean()),
                },
                "zoom": 13,
            },
            height=680,
            title=title,
            legend={"title": {"text": "Routes and buses"}, "x": 1.02, "y": 1, "xanchor": "left"},
            margin={"r": 300, "t": 45, "l": 0, "b": 0},
        )
        if line_count == 0:
            st.info("Route geometry is unavailable. Run fetch_reference.py or allow the dashboard to reach the GT route API.")
    else:
        figure = px.scatter(
            latest,
            x="longitude",
            y="latitude",
            color="route_id",
            hover_name="vehicle_name",
            hover_data=hover_data,
            height=600,
            title=f"{title} — longitude/latitude view",
        )
        figure.update_yaxes(scaleanchor="x", scaleratio=1)

    if map_view == "Offline coordinate view" and not routes_by_id:
        st.info("Route geometry is unavailable in the offline view. Run fetch_reference.py to add colored route lines.")
    st.plotly_chart(figure, width="stretch")


def main() -> None:
    st.set_page_config(page_title="GT Bus Heading Dashboard", layout="wide")
    st.title("GT Bus Heading Dashboard")
    st.caption("Vehicle-position observations collected by vehicle_heading_collector.py")

    if st.sidebar.button("Refresh data"):
        st.cache_data.clear()
        st.rerun()

    data = load_points(str(DATA_DIR))
    if data.empty:
        st.warning(f"No vehicle CSV files found in `{DATA_DIR}`.")
        st.code("python3 vehicle_heading_collector.py\nstreamlit run dashboard.py")
        st.stop()

    filtered = filter_data(data)
    if filtered.empty:
        st.warning("No rows match the selected filters.")
        st.stop()

    median_interval, gap_count, max_gap = polling_stats(filtered)
    metrics = st.columns(6)
    metrics[0].metric("Observations", f"{len(filtered):,}")
    metrics[1].metric("Vehicles", f"{filtered['vehicle_id'].nunique():,}")
    metrics[2].metric("Routes", f"{filtered['route_id'].nunique():,}")
    metrics[3].metric("Median poll interval", f"{median_interval:.1f}s" if median_interval else "n/a")
    metrics[4].metric("Longest poll gap", f"{max_gap:.1f}s" if max_gap else "n/a")
    metrics[5].metric("Poll gaps > 7.5s", f"{gap_count:,}")

    render_map(filtered)

    left, right = st.columns(2)
    with left:
        info_header(
            "Active buses over time",
            "Counts distinct vehicle IDs seen in each minute. Use this to check service coverage and whether "
            "the collector is receiving a normal number of buses; it does not measure direction.",
        )
        active = (
            filtered.set_index("fetched_at")
            .groupby(pd.Grouper(freq="1min"))["vehicle_id"]
            .nunique()
            .rename("vehicles")
            .reset_index()
        )
        st.plotly_chart(px.line(active, x="fetched_at", y="vehicles"), width="stretch")

    with right:
        info_header(
            "Average speed by route",
            "Shows the mean GroundSpeed reported for each route. It helps identify slower or faster routes, "
            "but speed alone does not tell us which direction a bus is traveling.",
        )
        speed = (
            filtered.groupby("route_id", dropna=False)["ground_speed"]
            .mean()
            .reset_index()
        )
        speed = speed.rename(columns={"ground_speed": "average_speed"})
        speed["route_label"] = "Route " + speed["route_id"].astype(str)
        speed = speed.sort_values("route_id")
        speed_figure = px.bar(
            speed,
            x="route_label",
            y="average_speed",
            color="route_label",
            text="average_speed",
            category_orders={"route_label": speed["route_label"].tolist()},
            color_discrete_sequence=px.colors.qualitative.Set2,
            labels={"route_label": "Route ID", "average_speed": "Average ground speed"},
        )
        speed_figure.update_traces(texttemplate="%{text:.2f}", textposition="outside")
        speed_figure.update_layout(showlegend=False, xaxis={"type": "category"})
        st.plotly_chart(speed_figure, width="stretch")

    left, right = st.columns(2)
    with left:
        info_header(
            "Heading distribution",
            "Groups every heading into a compass bucket: north, northeast, east, and so on. This shows the "
            "overall orientation of observations, but a route’s true direction may change as it curves.",
        )
        heading_counts = (
            filtered["cardinal_direction"]
            .value_counts()
            .reindex(["N", "NE", "E", "SE", "S", "SW", "W", "NW", "Unknown"], fill_value=0)
            .rename_axis("direction")
            .reset_index(name="observations")
        )
        st.plotly_chart(px.bar(heading_counts, x="direction", y="observations"), width="stretch")

    with right:
        info_header(
            "Source age",
            "Shows the API Seconds value, which indicates how old the upstream GPS observation was when it "
            "was returned. Smaller values mean fresher position data.",
        )
        st.plotly_chart(px.histogram(filtered, x="seconds", nbins=20, labels={"seconds": "API source age (seconds)"}), width="stretch")

    left, right = st.columns(2)
    with left:
        info_header(
            "Heading over time",
            "Plots the raw compass bearing for one selected bus over time. This helps reveal turns, route "
            "changes, and stale or noisy readings. A jump between 359° and 0° is a normal compass wraparound, "
            "not necessarily a sharp turn.",
        )
        heading_vehicles = sorted(filtered["vehicle_id"].dropna().astype(str).unique().tolist())
        selected_heading_vehicle = st.selectbox("Bus", heading_vehicles, key="heading_timeline_vehicle")
        vehicle_heading = filtered[filtered["vehicle_id"] == selected_heading_vehicle]
        heading_figure = px.scatter(
            vehicle_heading,
            x="fetched_at",
            y="heading",
            color="route_id",
            hover_data=["vehicle_name", "ground_speed", "seconds"],
            labels={"heading": "Heading (degrees clockwise from north)", "fetched_at": "Time"},
        )
        heading_figure.update_yaxes(range=[0, 360], dtick=45)
        st.plotly_chart(heading_figure, width="stretch")

    with right:
        info_header(
            "Direction mix by route",
            "Shows the percentage of observations in each compass direction for each route. It is useful for "
            "comparing route behavior, but forward/reverse route direction still requires matching positions to "
            "the route geometry and stop order.",
        )
        direction_by_route = (
            filtered.groupby(["route_id", "cardinal_direction"], dropna=False)
            .size()
            .reset_index(name="observations")
        )
        direction_by_route["share_percent"] = direction_by_route["observations"] / direction_by_route.groupby("route_id")["observations"].transform("sum") * 100
        direction_by_route["route_label"] = "Route " + direction_by_route["route_id"].astype(str)
        direction_figure = px.bar(
            direction_by_route,
            x="route_label",
            y="share_percent",
            color="cardinal_direction",
            barmode="stack",
            category_orders={"route_label": sorted(direction_by_route["route_label"].unique())},
            labels={"route_label": "Route ID", "share_percent": "Share of observations (%)", "cardinal_direction": "Heading"},
        )
        direction_figure.update_layout(yaxis={"ticksuffix": "%"})
        st.plotly_chart(direction_figure, width="stretch")

    info_header(
        "Route summary",
        "Summarizes how much data was collected per route, how many unique buses appeared, average speed, "
        "and average GPS source age. Use it to compare coverage and data quality across routes.",
    )
    route_summary = (
        filtered.groupby("route_id", dropna=False)
        .agg(
            observations=("vehicle_id", "size"),
            vehicles=("vehicle_id", "nunique"),
            average_speed=("ground_speed", "mean"),
            median_speed=("ground_speed", "median"),
            average_source_age=("seconds", "mean"),
        )
        .reset_index()
    )
    st.dataframe(route_summary, width="stretch", hide_index=True)


if __name__ == "__main__":
    main()
