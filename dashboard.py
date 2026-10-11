"""Local Streamlit dashboard for vehicle heading CSV files.

Run from the repository root with:

    streamlit run dashboard.py
"""

import json
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "vehicle_heading"
ROUTES_PATH = ROOT / "reference" / "routes.json"
ROUTES_URL = "https://bus.gatech.edu/Services/JSONPRelay.svc/GetRoutesForMapWithScheduleWithEncodedLine"
INCOMPLETE_DATES = {date(2026, 10, 9)}
PREFERRED_START_DATE = date(2026, 10, 2)
PREFERRED_END_DATE = date(2026, 10, 8)
MOVING_SPEED_THRESHOLD = 3.0
MIN_MOVEMENT_DISTANCE_METERS = 10.0
MAX_MOVEMENT_INTERVAL_SECONDS = 120.0
RECENT_HEADING_SECONDS = 30.0
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


def angular_difference(first: pd.Series, second: pd.Series) -> pd.Series:
    """Return the smallest absolute difference between two compass bearings."""
    return ((first - second + 180) % 360 - 180).abs()


def bearing_between(
    start_latitude: pd.Series,
    start_longitude: pd.Series,
    end_latitude: pd.Series,
    end_longitude: pd.Series,
) -> pd.Series:
    """Calculate a true-north bearing from one coordinate to another."""
    start_latitude_radians = np.radians(start_latitude.to_numpy(dtype="float64"))
    end_latitude_radians = np.radians(end_latitude.to_numpy(dtype="float64"))
    longitude_delta_radians = np.radians(
        (end_longitude - start_longitude).to_numpy(dtype="float64")
    )
    y = np.sin(longitude_delta_radians) * np.cos(end_latitude_radians)
    x = (
        np.cos(start_latitude_radians) * np.sin(end_latitude_radians)
        - np.sin(start_latitude_radians)
        * np.cos(end_latitude_radians)
        * np.cos(longitude_delta_radians)
    )
    values = (np.degrees(np.arctan2(y, x)) + 360) % 360
    return pd.Series(values, index=start_latitude.index)


def distance_between_meters(
    start_latitude: pd.Series,
    start_longitude: pd.Series,
    end_latitude: pd.Series,
    end_longitude: pd.Series,
) -> pd.Series:
    """Calculate great-circle distance between two coordinate pairs."""
    earth_radius_meters = 6_371_000.0
    start_latitude_radians = np.radians(start_latitude.to_numpy(dtype="float64"))
    end_latitude_radians = np.radians(end_latitude.to_numpy(dtype="float64"))
    latitude_delta_radians = end_latitude_radians - start_latitude_radians
    longitude_delta_radians = np.radians(
        (end_longitude - start_longitude).to_numpy(dtype="float64")
    )
    haversine = (
        np.sin(latitude_delta_radians / 2) ** 2
        + np.cos(start_latitude_radians)
        * np.cos(end_latitude_radians)
        * np.sin(longitude_delta_radians / 2) ** 2
    )
    values = 2 * earth_radius_meters * np.arcsin(np.sqrt(np.clip(haversine, 0, 1)))
    return pd.Series(values, index=start_latitude.index)


def add_movement_features(data: pd.DataFrame) -> pd.DataFrame:
    """Add movement-derived heading and data-quality fields.

    The API heading is retained as ``heading``. ``movement_heading`` is the
    bearing calculated from consecutive GPS positions for the same vehicle.
    ``inferred_heading`` uses the current movement bearing when available and
    otherwise carries forward the last reliable movement bearing. This is a
    useful fallback while a bus is stopped, but it is not a measurement of the
    bus's physical nose orientation.
    """
    if data.empty:
        return data

    ordered = data.sort_values(["vehicle_id", "fetched_at"]).copy()
    by_vehicle = ordered.groupby("vehicle_id", sort=False)
    ordered["previous_latitude"] = by_vehicle["latitude"].shift()
    ordered["previous_longitude"] = by_vehicle["longitude"].shift()
    ordered["position_interval_seconds"] = (
        by_vehicle["fetched_at"].diff().dt.total_seconds()
    )

    valid_pair = (
        ordered["latitude"].notna()
        & ordered["longitude"].notna()
        & ordered["previous_latitude"].notna()
        & ordered["previous_longitude"].notna()
        & ordered["position_interval_seconds"].gt(0)
        & ordered["position_interval_seconds"].le(MAX_MOVEMENT_INTERVAL_SECONDS)
    )
    ordered["position_delta_meters"] = np.nan
    ordered.loc[valid_pair, "position_delta_meters"] = distance_between_meters(
        ordered.loc[valid_pair, "previous_latitude"],
        ordered.loc[valid_pair, "previous_longitude"],
        ordered.loc[valid_pair, "latitude"],
        ordered.loc[valid_pair, "longitude"],
    )

    movement_pair = valid_pair & ordered["position_delta_meters"].ge(
        MIN_MOVEMENT_DISTANCE_METERS
    )
    ordered["movement_heading"] = np.nan
    ordered.loc[movement_pair, "movement_heading"] = bearing_between(
        ordered.loc[movement_pair, "previous_latitude"],
        ordered.loc[movement_pair, "previous_longitude"],
        ordered.loc[movement_pair, "latitude"],
        ordered.loc[movement_pair, "longitude"],
    )
    ordered["heading_difference_degrees"] = np.nan
    comparable = movement_pair & ordered["heading"].notna()
    ordered.loc[comparable, "heading_difference_degrees"] = angular_difference(
        ordered.loc[comparable, "heading"],
        ordered.loc[comparable, "movement_heading"],
    )
    ordered["heading_agreement"] = "Not comparable"
    ordered.loc[
        comparable & ordered["heading_difference_degrees"].le(30),
        "heading_agreement",
    ] = "Agree (≤30°)"
    ordered.loc[
        comparable
        & ordered["heading_difference_degrees"].gt(30)
        & ordered["heading_difference_degrees"].le(90),
        "heading_agreement",
    ] = "Partial (31–90°)"
    ordered.loc[
        comparable & ordered["heading_difference_degrees"].gt(90),
        "heading_agreement",
    ] = "Disagree (>90°)"

    ordered["motion_state"] = "Unknown"
    known_interval = ordered["position_interval_seconds"].le(
        MAX_MOVEMENT_INTERVAL_SECONDS
    )
    ordered.loc[
        known_interval
        & (
            ordered["ground_speed"].ge(MOVING_SPEED_THRESHOLD)
            | ordered["movement_heading"].notna()
        ),
        "motion_state",
    ] = "Moving"
    ordered.loc[
        known_interval
        & ordered["ground_speed"].lt(MOVING_SPEED_THRESHOLD)
        & ordered["position_delta_meters"].fillna(0).lt(MIN_MOVEMENT_DISTANCE_METERS),
        "motion_state",
    ] = "Stopped"

    last_moving_heading = ordered["movement_heading"].where(movement_pair)
    last_moving_time = ordered["fetched_at"].where(movement_pair)
    ordered["last_moving_heading"] = last_moving_heading.groupby(
        ordered["vehicle_id"], sort=False
    ).ffill()
    ordered["last_moving_at"] = last_moving_time.groupby(
        ordered["vehicle_id"], sort=False
    ).ffill()
    ordered["heading_age_seconds"] = (
        ordered["fetched_at"] - ordered["last_moving_at"]
    ).dt.total_seconds()

    ordered["inferred_heading"] = ordered["movement_heading"]
    ordered["heading_source"] = "Unknown"
    ordered.loc[movement_pair, "heading_source"] = "Current movement"
    use_last_movement = ~movement_pair & ordered["last_moving_heading"].notna()
    ordered.loc[use_last_movement, "inferred_heading"] = ordered.loc[
        use_last_movement, "last_moving_heading"
    ]
    ordered.loc[use_last_movement, "heading_source"] = "Last movement"
    use_api_heading = (
        ordered["inferred_heading"].isna() & ordered["heading"].notna()
    )
    ordered.loc[use_api_heading, "inferred_heading"] = ordered.loc[
        use_api_heading, "heading"
    ]
    ordered.loc[use_api_heading, "heading_source"] = "API heading"

    ordered["heading_confidence"] = "Unknown"
    ordered.loc[movement_pair, "heading_confidence"] = "High"
    ordered.loc[
        use_last_movement & ordered["heading_age_seconds"].le(RECENT_HEADING_SECONDS),
        "heading_confidence",
    ] = "Medium"
    ordered.loc[
        use_last_movement & ordered["heading_age_seconds"].gt(RECENT_HEADING_SECONDS),
        "heading_confidence",
    ] = "Low"
    ordered.loc[use_api_heading, "heading_confidence"] = "Low"

    ordered["cardinal_direction"] = ordered["heading"].map(cardinal_direction)
    ordered["inferred_cardinal_direction"] = ordered["inferred_heading"].map(
        cardinal_direction
    )
    return ordered.sort_index()


@st.cache_data(ttl=5)
def load_points(data_dir: str) -> pd.DataFrame:
    paths = sorted(Path(data_dir).glob("vehicle_points_*.csv"))
    if not paths:
        return pd.DataFrame(columns=EXPECTED_COLUMNS)

    dtype = {
        "tick": "Int64",
        "source_timestamp": "string",
        "seconds": "float32",
        "vehicle_id": "string",
        "vehicle_name": "string",
        "route_id": "string",
        "latitude": "float32",
        "longitude": "float32",
        "ground_speed": "float32",
        "heading": "float32",
    }
    frames = [
        pd.read_csv(
            path,
            usecols=EXPECTED_COLUMNS,
            dtype=dtype,
        )
        for path in paths
        if path.stat().st_size > 0
    ]
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
    return add_movement_features(data)


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


@st.cache_data(ttl=3600)
def add_stop_context(data: pd.DataFrame, routes_by_id: dict[str, dict]) -> pd.DataFrame:
    """Match each observation to its nearest route stop and stop sequence."""
    if data.empty or not routes_by_id:
        return data

    enriched = data.copy()
    string_columns = [
        "nearest_stop_id",
        "nearest_stop_name",
        "previous_stop_id",
        "next_stop_id",
        "route_direction_estimate",
        "route_direction_confidence",
    ]
    for column in string_columns:
        enriched[column] = "Unknown"
    for column in (
        "distance_to_stop_meters",
        "route_forward_heading",
        "route_reverse_heading",
        "route_heading_alignment_degrees",
    ):
        enriched[column] = np.nan

    for route_id, row_indices in enriched.groupby("route_id", dropna=False).groups.items():
        route = routes_by_id.get(str(route_id), {})
        stops = [
            stop
            for stop in route.get("Stops", [])
            if stop.get("Latitude") is not None and stop.get("Longitude") is not None
        ]
        if not stops:
            continue

        observations = enriched.loc[list(row_indices)].dropna(
            subset=["latitude", "longitude"]
        )
        if observations.empty:
            continue

        observation_latitudes = np.radians(observations["latitude"].to_numpy(dtype="float64"))
        observation_longitudes = np.radians(observations["longitude"].to_numpy(dtype="float64"))
        stop_latitudes = np.radians(
            np.array([float(stop["Latitude"]) for stop in stops], dtype="float64")
        )
        stop_longitudes = np.radians(
            np.array([float(stop["Longitude"]) for stop in stops], dtype="float64")
        )
        latitude_delta = observation_latitudes[:, None] - stop_latitudes[None, :]
        longitude_delta = observation_longitudes[:, None] - stop_longitudes[None, :]
        mean_latitude = (observation_latitudes[:, None] + stop_latitudes[None, :]) / 2
        distance_matrix = 6_371_000.0 * np.sqrt(
            latitude_delta**2
            + (longitude_delta * np.cos(mean_latitude)) ** 2
        )
        nearest_indexes = np.argmin(distance_matrix, axis=1)
        nearest_distances = distance_matrix[
            np.arange(len(observations)), nearest_indexes
        ]

        observation_index = observations.index
        nearest_stops = [stops[index] for index in nearest_indexes]
        enriched.loc[observation_index, "nearest_stop_id"] = [
            str(stop.get("RouteStopID", "Unknown")) for stop in nearest_stops
        ]
        enriched.loc[observation_index, "nearest_stop_name"] = [
            str(stop.get("Description") or stop.get("Name") or "Unknown")
            for stop in nearest_stops
        ]
        enriched.loc[observation_index, "distance_to_stop_meters"] = nearest_distances

        next_stops = [
            stops[index + 1] if index + 1 < len(stops) else None
            for index in nearest_indexes
        ]
        previous_stops = [
            stops[index - 1] if index > 0 else None for index in nearest_indexes
        ]
        enriched.loc[observation_index, "next_stop_id"] = [
            str(stop.get("RouteStopID")) if stop else "Unknown" for stop in next_stops
        ]
        enriched.loc[observation_index, "previous_stop_id"] = [
            str(stop.get("RouteStopID")) if stop else "Unknown"
            for stop in previous_stops
        ]

        nearest_latitude = pd.Series(
            [float(stop["Latitude"]) for stop in nearest_stops], index=observation_index
        )
        nearest_longitude = pd.Series(
            [float(stop["Longitude"]) for stop in nearest_stops], index=observation_index
        )
        next_latitude = pd.Series(
            [float(stop["Latitude"]) if stop else np.nan for stop in next_stops],
            index=observation_index,
        )
        next_longitude = pd.Series(
            [float(stop["Longitude"]) if stop else np.nan for stop in next_stops],
            index=observation_index,
        )
        previous_latitude = pd.Series(
            [float(stop["Latitude"]) if stop else np.nan for stop in previous_stops],
            index=observation_index,
        )
        previous_longitude = pd.Series(
            [float(stop["Longitude"]) if stop else np.nan for stop in previous_stops],
            index=observation_index,
        )
        forward_available = next_latitude.notna()
        reverse_available = previous_latitude.notna()
        enriched.loc[observation_index, "route_forward_heading"] = bearing_between(
            nearest_latitude,
            nearest_longitude,
            next_latitude,
            next_longitude,
        ).where(forward_available)
        enriched.loc[observation_index, "route_reverse_heading"] = bearing_between(
            nearest_latitude,
            nearest_longitude,
            previous_latitude,
            previous_longitude,
        ).where(reverse_available)

        inferred_heading = enriched.loc[observation_index, "inferred_heading"]
        forward_heading = enriched.loc[observation_index, "route_forward_heading"]
        reverse_heading = enriched.loc[observation_index, "route_reverse_heading"]
        forward_difference = angular_difference(inferred_heading, forward_heading)
        reverse_difference = angular_difference(inferred_heading, reverse_heading)
        choose_forward = (
            inferred_heading.notna()
            & forward_heading.notna()
            & (reverse_heading.isna() | (forward_difference <= reverse_difference))
        )
        choose_reverse = (
            inferred_heading.notna()
            & reverse_heading.notna()
            & (forward_heading.isna() | (reverse_difference < forward_difference))
        )
        enriched.loc[choose_forward.index[choose_forward], "route_direction_estimate"] = (
            "Toward next stop"
        )
        enriched.loc[choose_reverse.index[choose_reverse], "route_direction_estimate"] = (
            "Toward previous stop"
        )
        alignment = pd.concat(
            [forward_difference.where(choose_forward), reverse_difference.where(choose_reverse)],
            axis=1,
        ).min(axis=1)
        enriched.loc[observation_index, "route_heading_alignment_degrees"] = alignment
        enriched.loc[
            observation_index,
            "route_direction_confidence",
        ] = alignment.map(
            lambda value: (
                "High"
                if pd.notna(value) and value <= 45
                else "Medium"
                if pd.notna(value) and value <= 90
                else "Low"
                if pd.notna(value)
                else "Unknown"
            )
        )

    return enriched


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
        api_heading = row["heading"]
        inferred_heading = row["inferred_heading"]
        heading = (
            float(inferred_heading)
            if pd.notna(inferred_heading)
            else float(api_heading)
            if pd.notna(api_heading)
            else 0.0
        )
        speed = row["ground_speed"]
        source_age = row["seconds"]
        direction = row["inferred_cardinal_direction"]
        heading_source = row["heading_source"]
        heading_confidence = row["heading_confidence"]
        motion_state = row["motion_state"]
        nearest_stop = row["nearest_stop_name"]
        route_direction = row["route_direction_estimate"]
        bus_label = f"Bus {vehicle_name} — Route {route_id}"
        hover = (
            f"<b>{bus_label}</b><br>"
            f"Vehicle ID: {vehicle_id}<br>"
            f"Displayed heading: {heading:.0f}° ({direction})<br>"
            f"API heading: {float(api_heading):.0f}°<br>"
            f"Heading source: {heading_source}<br>"
            f"Heading confidence: {heading_confidence}<br>"
            f"Motion state: {motion_state}<br>"
            f"Ground speed: {speed:.2f}<br>"
            f"Source age: {source_age:.1f}s<br>"
            f"Nearest stop: {nearest_stop}<br>"
            f"Route direction: {route_direction}<extra></extra>"
            if pd.notna(speed) and pd.notna(source_age)
            else f"<b>{bus_label}</b><br>Vehicle ID: {vehicle_id}<br>Displayed heading: {heading:.0f}° ({direction})<br>Heading source: {heading_source}<br>Heading confidence: {heading_confidence}<br>Motion state: {motion_state}<extra></extra>"
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
    dates = data["fetched_at"].dt.date
    usable_dates = sorted(set(dates.dropna()) - INCOMPLETE_DATES)
    if not usable_dates:
        st.error("No complete dates are available. The 2026-10-09 collection is intentionally excluded.")
        return data.iloc[0:0]

    filtered = data[~dates.isin(INCOMPLETE_DATES)]

    preferred_dates = [
        value for value in usable_dates
        if PREFERRED_START_DATE <= value <= PREFERRED_END_DATE
    ]
    default_start = min(preferred_dates) if preferred_dates else min(usable_dates)
    default_end = max(preferred_dates) if preferred_dates else max(usable_dates)
    date_range = st.sidebar.date_input(
        "Date range",
        value=(default_start, default_end),
        min_value=min(usable_dates),
        max_value=max(usable_dates),
        help="The incomplete 2026-10-09 collection is excluded. The default view is the complete 2026-10-02 through 2026-10-08 window.",
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
        "the arrow uses the current movement heading when there is enough position change, then falls back "
        "to the last reliable movement heading or the API heading. The legend identifies both routes and buses. "
        "A stopped bus's physical nose orientation cannot be proven from GPS positions alone.",
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
        "inferred_heading": True,
        "heading_source": True,
        "heading_confidence": True,
        "motion_state": True,
        "route_direction_estimate": True,
        "nearest_stop_name": True,
        "distance_to_stop_meters": True,
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

    routes = load_routes(str(ROUTES_PATH))
    routes_by_id = {
        str(route.get("RouteID")): route
        for route in routes
        if route.get("RouteID") is not None
    }
    data = add_stop_context(data, routes_by_id)
    filtered = filter_data(data)
    if filtered.empty:
        st.warning("No rows match the selected filters.")
        st.stop()

    median_interval, gap_count, max_gap = polling_stats(filtered)
    heading_comparable = filtered["heading_agreement"] != "Not comparable"
    high_confidence = filtered["heading_confidence"] == "High"
    metrics = st.columns(8)
    metrics[0].metric("Observations", f"{len(filtered):,}")
    metrics[1].metric("Vehicles", f"{filtered['vehicle_id'].nunique():,}")
    metrics[2].metric("Routes", f"{filtered['route_id'].nunique():,}")
    metrics[3].metric("Median poll interval", f"{median_interval:.1f}s" if median_interval else "n/a")
    metrics[4].metric("Longest poll gap", f"{max_gap:.1f}s" if max_gap else "n/a")
    metrics[5].metric("Poll gaps > 7.5s", f"{gap_count:,}")
    metrics[6].metric("Heading comparisons", f"{heading_comparable.sum():,}")
    metrics[7].metric("High-confidence headings", f"{high_confidence.sum():,}")

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
            "Inferred heading distribution",
            "Groups the best available heading into compass buckets. Moving buses use the bearing calculated "
            "from consecutive GPS positions; stopped buses use the last reliable movement bearing when one "
            "exists. This is travel direction, not guaranteed physical nose orientation.",
        )
        heading_counts = (
            filtered["inferred_cardinal_direction"]
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
            "API heading versus movement heading",
            "Compares the API-provided heading with the bearing calculated from consecutive GPS positions. "
            "Large differences indicate noisy, stale, or otherwise questionable API headings. Compass wraparound "
            "between 359° and 0° is normal.",
        )
        heading_vehicles = sorted(filtered["vehicle_id"].dropna().astype(str).unique().tolist())
        selected_heading_vehicle = st.selectbox("Bus", heading_vehicles, key="heading_timeline_vehicle")
        vehicle_heading = filtered[filtered["vehicle_id"] == selected_heading_vehicle]
        heading_figure = go.Figure()
        heading_figure.add_trace(
            go.Scatter(
                x=vehicle_heading["fetched_at"],
                y=vehicle_heading["heading"],
                mode="markers",
                name="API heading",
                marker={"size": 5, "opacity": 0.55},
                customdata=vehicle_heading[["ground_speed", "motion_state"]],
                hovertemplate=(
                    "API heading: %{y:.0f}°<br>Speed: %{customdata[0]:.1f}<br>"
                    "Motion: %{customdata[1]}<extra></extra>"
                ),
            )
        )
        heading_figure.add_trace(
            go.Scatter(
                x=vehicle_heading["fetched_at"],
                y=vehicle_heading["movement_heading"],
                mode="markers",
                name="Movement heading",
                marker={"size": 6, "color": "#d62728"},
                hovertemplate="Movement heading: %{y:.0f}°<extra></extra>",
            )
        )
        heading_figure.update_layout(
            xaxis_title="Time",
            yaxis_title="Heading (degrees clockwise from north)",
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
            filtered.groupby(["route_id", "inferred_cardinal_direction"], dropna=False)
            .size()
            .reset_index(name="observations")
        )
        direction_by_route["share_percent"] = direction_by_route["observations"] / direction_by_route.groupby("route_id")["observations"].transform("sum") * 100
        direction_by_route["route_label"] = "Route " + direction_by_route["route_id"].astype(str)
        direction_figure = px.bar(
            direction_by_route,
            x="route_label",
            y="share_percent",
            color="inferred_cardinal_direction",
            barmode="stack",
            category_orders={"route_label": sorted(direction_by_route["route_label"].unique())},
            labels={"route_label": "Route ID", "share_percent": "Share of observations (%)", "inferred_cardinal_direction": "Heading"},
        )
        direction_figure.update_layout(yaxis={"ticksuffix": "%"})
        st.plotly_chart(direction_figure, width="stretch")

    left, right = st.columns(2)
    with left:
        info_header(
            "Heading agreement",
            "Counts observations where the API heading and GPS movement heading agree within 30 degrees, "
            "partially agree within 90 degrees, or disagree by more than 90 degrees. Only observations with "
            "enough movement between polls can be compared.",
        )
        agreement_order = [
            "Agree (≤30°)",
            "Partial (31–90°)",
            "Disagree (>90°)",
            "Not comparable",
        ]
        agreement = (
            filtered["heading_agreement"]
            .value_counts()
            .reindex(agreement_order, fill_value=0)
            .rename_axis("agreement")
            .reset_index(name="observations")
        )
        agreement_figure = px.bar(
            agreement,
            x="agreement",
            y="observations",
            color="agreement",
            category_orders={"agreement": agreement_order},
            labels={"agreement": "API vs. movement heading", "observations": "Observations"},
        )
        agreement_figure.update_layout(showlegend=False)
        st.plotly_chart(agreement_figure, width="stretch")

    with right:
        info_header(
            "Motion state and heading confidence",
            "Moving means the reported speed or GPS displacement indicates motion. Stopped means the bus has "
            "little reported speed and little position change. Heading confidence is high for a current movement "
            "bearing, medium for a recent carried-forward bearing, and low for an old or API-only heading.",
        )
        state_counts = (
            filtered["motion_state"]
            .value_counts()
            .rename_axis("motion_state")
            .reset_index(name="observations")
        )
        state_counts["type"] = "Motion state"
        confidence_counts = (
            filtered["heading_confidence"]
            .value_counts()
            .rename_axis("motion_state")
            .reset_index(name="observations")
        )
        confidence_counts["type"] = "Heading confidence"
        state_figure = px.bar(
            pd.concat([state_counts, confidence_counts], ignore_index=True),
            x="motion_state",
            y="observations",
            color="type",
            barmode="group",
            labels={"motion_state": "State or confidence", "observations": "Observations"},
        )
        st.plotly_chart(state_figure, width="stretch")

    left, right = st.columns(2)
    with left:
        info_header(
            "Route-sequence direction",
            "Uses the ordered stops in reference/routes.json. For each observation, the inferred heading is "
            "compared with the bearing toward the next and previous stop. This is a route-relative estimate, "
            "not a perfect inbound/outbound label, especially near loops, terminals, or route geometry gaps.",
        )
        route_direction = (
            filtered.groupby(["route_id", "route_direction_estimate"], dropna=False)
            .size()
            .reset_index(name="observations")
        )
        route_direction["route_label"] = "Route " + route_direction["route_id"].astype(str)
        route_direction_figure = px.bar(
            route_direction,
            x="route_label",
            y="observations",
            color="route_direction_estimate",
            barmode="stack",
            category_orders={
                "route_label": sorted(route_direction["route_label"].unique()),
                "route_direction_estimate": [
                    "Toward next stop",
                    "Toward previous stop",
                    "Unknown",
                ],
            },
            labels={
                "route_label": "Route ID",
                "observations": "Observations",
                "route_direction_estimate": "Estimated direction",
            },
        )
        st.plotly_chart(route_direction_figure, width="stretch")

    with right:
        info_header(
            "Heading age while stopped",
            "Shows how long a stopped bus has been relying on its last movement heading. A growing age means "
            "the dashboard is displaying a historical travel direction rather than a fresh movement measurement.",
        )
        stopped = filtered[
            (filtered["motion_state"] == "Stopped")
            & filtered["heading_age_seconds"].notna()
        ]
        if stopped.empty:
            st.info("No stopped observations with a previous movement heading are available for this selection.")
        else:
            age_figure = px.histogram(
                stopped,
                x="heading_age_seconds",
                nbins=20,
                labels={"heading_age_seconds": "Age of last movement heading (seconds)"},
            )
            st.plotly_chart(age_figure, width="stretch")

    info_header(
        "Route summary",
        "Summarizes how much data was collected per route, how many unique buses appeared, average speed, "
        "average GPS source age, movement coverage, heading agreement, and route-relative direction coverage. "
        "Use it to compare data quality across routes.",
    )
    route_summary = (
        filtered.groupby("route_id", dropna=False)
        .agg(
            observations=("vehicle_id", "size"),
            vehicles=("vehicle_id", "nunique"),
            average_speed=("ground_speed", "mean"),
            median_speed=("ground_speed", "median"),
            average_source_age=("seconds", "mean"),
            average_distance_to_stop_m=("distance_to_stop_meters", "mean"),
        )
        .reset_index()
    )
    route_summary["moving_share_percent"] = (
        filtered.assign(is_moving=filtered["motion_state"].eq("Moving"))
        .groupby("route_id", dropna=False)["is_moving"]
        .mean()
        .mul(100)
        .reindex(route_summary["route_id"])
        .to_numpy()
    )
    route_summary["heading_comparison_share_percent"] = (
        filtered.assign(is_comparable=heading_comparable)
        .groupby("route_id", dropna=False)["is_comparable"]
        .mean()
        .mul(100)
        .reindex(route_summary["route_id"])
        .to_numpy()
    )
    route_summary["heading_disagreement_share_percent"] = (
        filtered.assign(is_disagreement=filtered["heading_agreement"].eq("Disagree (>90°)"))
        .groupby("route_id", dropna=False)["is_disagreement"]
        .mean()
        .mul(100)
        .reindex(route_summary["route_id"])
        .to_numpy()
    )
    route_summary = route_summary.round(2)
    st.dataframe(route_summary, width="stretch", hide_index=True)


if __name__ == "__main__":
    main()
