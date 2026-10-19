"""Continuously compare frozen stop-arrival means with GPS outcomes and TransLoc.

SQLite checkpoints make each received observation, its state changes, and its
scores one transaction. CSVs are flushed after each update and rebuilt from
that checkpoint on resume, including recovery from an interrupted CSV append.
Only Python's standard library is required.
"""

import argparse
import bisect
import csv
import hashlib
import json
import logging
import math
import os
import random
import re
import signal
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    from .build_predictions import BUCKETS, LOCAL, ROOT, atomic_csv, load_routes, parse_time
except ImportError:
    from build_predictions import BUCKETS, LOCAL, ROOT, atomic_csv, load_routes, parse_time

BASE_URL = "https://bus.gatech.edu/Services/JSONPRelay.svc"
SOURCE_TIME = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")
PREDICTION_COLUMNS = [
    "prediction_id", "origin_event_id", "route_id", "vehicle_id", "vehicle_name",
    "origin_route_stop_id", "destination_route_stop_id", "horizon_stops",
    "bucket_minutes", "bucket_start_minute", "origin_arrival", "predicted_arrival",
    "mean_elapsed_seconds", "sample_count", "service_date_count", "fallback_used",
    "transloc_predicted_arrival", "transloc_received_at", "transloc_status",
    "service_date", "reporting_hour", "model_hash",
]
RESULT_COLUMNS = PREDICTION_COLUMNS + [
    "status", "reason", "actual_arrival", "actual_lower_bound", "actual_elapsed_seconds",
    "error_seconds", "absolute_error_seconds", "transloc_error_seconds",
    "transloc_absolute_error_seconds", "target_sequence", "finished_at",
]
ARRIVAL_COLUMNS = [
    "event_id", "route_id", "vehicle_id", "vehicle_name", "route_stop_id",
    "stop_index", "sequence", "observed_arrival", "lower_bound", "source_timestamp",
]
SUMMARY_COLUMNS = [
    "scope", "route_id", "horizon_stops", "bucket_minutes", "reporting_hour",
    "created_count", "resolved_count", "excluded_count", "unavailable_count",
    "pending_count", "fallback_count", "forecast_coverage", "resolution_coverage",
    "mae_seconds", "median_absolute_error_seconds", "p90_absolute_error_seconds",
    "bias_seconds", "within_30_seconds", "within_60_seconds", "within_120_seconds",
    "paired_count", "paired_model_mae_seconds", "paired_transloc_mae_seconds",
    "paired_improvement_seconds", "transloc_coverage", "all_variants_common_count",
    "all_variants_common_mae_seconds", "updated_at",
]
SELECTION_COLUMNS = [
    "route_id", "bucket_minutes", "supported_horizons", "unsupported_horizons",
    "equal_horizon_mae_seconds", "best_observed", "paired_improvement_seconds",
    "paired_improvement_ci_low", "paired_improvement_ci_high", "paired_service_dates",
    "performance_label",
]


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def utc_now():
    return datetime.now(timezone.utc)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def distance(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    h = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742000 * math.asin(math.sqrt(min(1, max(0, h))))


def quantile(sorted_values, fraction):
    if not sorted_values:
        return ""
    position = (len(sorted_values) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (position - lo)


def source_timestamp(value):
    """Parse source dates without assuming every API field shares our clock.

    EstimateTime is an absolute arrival prediction. GPS TimeStamp on this
    deployment is only a source ordering token: its clock is offset from UTC
    and even its Seconds field counts down on repeated fixes. GPS freshness
    must therefore be measured using local receipts of advancing source fixes.
    """
    match = SOURCE_TIME.fullmatch(str(value))
    if match:
        return int(match[1]) / 1000
    return parse_time(value).timestamp()


class Model:
    def __init__(self, path, routes, min_samples=20, min_dates=3):
        self.hash = digest(path)
        self.routes = routes
        self.min_samples = min_samples
        self.min_dates = min_dates
        self.means = {}
        self.training_end = None
        with Path(path).open(newline="", encoding="utf-8") as source:
            for row in csv.DictReader(source):
                rid = row["route_id"]
                if rid not in routes or row["route_definition_hash"] != routes[rid]["hash"]:
                    raise ValueError(f"Route {rid} differs from the frozen model; rebuild averages")
                n = len(routes[rid]["stops"])
                origin, destination, horizon, width = (int(row[k]) for k in (
                    "origin_order", "destination_order", "horizon_stops", "bucket_minutes"))
                count, dates = int(row["sample_count"]), int(row["service_date_count"])
                mean = float(row["mean_elapsed_seconds"])
                if (not 0 <= origin < n or not 1 <= horizon <= n or destination != (origin + horizon) % n
                        or int(row["route_stop_count"]) != n or width not in BUCKETS
                        or count <= 0 or not 0 < dates <= count or not math.isfinite(mean) or mean <= 0
                        or row["origin_route_stop_id"] != routes[rid]["stops"][origin]["id"]
                        or row["destination_route_stop_id"] != routes[rid]["stops"][destination]["id"]):
                    raise ValueError("Invalid averages row")
                bucket = int(row["bucket_start_minute"]) if width else -1
                if width and (not 0 <= bucket < 1440 or bucket % width):
                    raise ValueError("Invalid time bucket")
                key = (rid, origin, horizon, width, bucket)
                if key in self.means:
                    raise ValueError(f"Duplicate averages key: {key}")
                self.means[key] = (mean, count, dates)
                end = parse_time(row["training_data_end"])
                self.training_end = max(self.training_end, end) if self.training_end else end
        if not self.means:
            raise ValueError("Averages CSV is empty")

    def lookup(self, rid, origin, horizon, width, at):
        local = at.astimezone(LOCAL)
        minute = local.hour * 60 + local.minute
        bucket = minute // width * width if width else -1
        item = self.means.get((rid, origin, horizon, width, bucket))
        fallback = False
        if width and (item is None or item[1] < self.min_samples or item[2] < self.min_dates):
            item = self.means.get((rid, origin, horizon, 0, -1))
            fallback = True
        return item, fallback, bucket


class Metrics:
    def __init__(self):
        self.created = self.fallback = self.resolved = self.excluded = self.unavailable = 0
        self.errors = []
        self.total_error = 0.0
        self.bias = self.paired_model = self.paired_vendor = 0.0
        self.paired = 0
        self.common_count = 0
        self.common_total = 0.0
        self.daily = defaultdict(lambda: [0.0, 0.0, 0])

    def add_created(self, row):
        self.created += 1
        self.fallback += int(row["fallback_used"])

    def add_result(self, row):
        status = row["status"]
        if status == "unavailable":
            self.unavailable += 1
        elif status == "excluded":
            self.excluded += 1
        elif status == "resolved":
            self.resolved += 1
            error = float(row["error_seconds"])
            bisect.insort(self.errors, abs(error))
            self.total_error += abs(error)
            self.bias += error
            if row["transloc_error_seconds"] != "":
                vendor = abs(float(row["transloc_error_seconds"]))
                self.paired += 1
                self.paired_model += abs(error)
                self.paired_vendor += vendor
                day = self.daily[row["service_date"]]
                day[0] += abs(error)
                day[1] += vendor
                day[2] += 1

    def record(self, key, at):
        scope, rid, horizon, width, hour = key
        n, paired = self.resolved, self.paired
        return dict(zip(SUMMARY_COLUMNS, [
            scope, rid, horizon, width, hour, self.created, n, self.excluded,
            self.unavailable, self.created - n - self.excluded - self.unavailable,
            self.fallback, (self.created - self.unavailable) / self.created if self.created else "",
            n / self.created if self.created else "",
            self.total_error / n if n else "", quantile(self.errors, .5), quantile(self.errors, .9),
            self.bias / n if n else "",
            *[bisect.bisect_right(self.errors, limit) / n if n else "" for limit in (30, 60, 120)],
            paired, self.paired_model / paired if paired else "",
            self.paired_vendor / paired if paired else "",
            (self.paired_vendor - self.paired_model) / paired if paired else "",
            paired / n if n else "", self.common_count,
            self.common_total / self.common_count if self.common_count else "", at,
        ]))


class Evaluator:
    def __init__(self, run_dir, model, routes_path, config):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # An OS lock prevents two processes from appending to the same run.
        import fcntl
        self.lock = (self.run_dir / ".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError("This evaluation directory is already in use")
        self.model = model
        self.routes = model.routes
        self.config = config
        self.db = sqlite3.connect(self.run_dir / "checkpoint.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, kind TEXT, received_at TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS arrivals (id TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE IF NOT EXISTS predictions (
                id TEXT PRIMARY KEY, vehicle TEXT, route TEXT, target INTEGER,
                status TEXT, payload TEXT, result_order INTEGER);
            CREATE INDEX IF NOT EXISTS pending_vehicle ON predictions(vehicle, status, target);
        """)
        frozen = {"model_hash": model.hash, "routes_hash": digest(routes_path), "config": config,
                  "min_samples": model.min_samples, "min_dates": model.min_dates, "format_version": 3}
        existing = self.db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()
        if existing and json.loads(existing[0]) != frozen:
            previous = json.loads(existing[0])
            previous_version = previous.pop("format_version", None)
            current = {key: value for key, value in frozen.items() if key != "format_version"}
            empty_results = (not self.db.execute("SELECT 1 FROM predictions LIMIT 1").fetchone()
                             and not self.db.execute("SELECT 1 FROM arrivals LIMIT 1").fetchone())
            if previous_version == 2 and previous == current and empty_results:
                # The broken clock check produced no GPS events in these runs.
                # They can resume safely without mixing old and new scores.
                with self.db:
                    self.db.execute("UPDATE metadata SET value=? WHERE key='frozen'", (json.dumps(frozen),))
                    self.db.execute("DELETE FROM metadata WHERE key='state'")
                logging.info("Upgraded empty run to local-observation GPS freshness")
            else:
                self.close()
                raise ValueError("Run settings/model or GPS clock policy changed; use a new --run-dir. "
                                 "Replay the raw journal to recover results with the corrected policy.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('frozen', ?)", (json.dumps(frozen),))
        state = self.db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
        saved = json.loads(state[0]) if state else {}
        self.vehicles = saved.get("vehicles", {})
        self.stale_sources = saved.get("stale_sources", {})
        self.estimates = saved.get("estimates", {})
        self.last_at = saved.get("last_at", "")
        self.result_order = self.db.execute("SELECT COALESCE(MAX(result_order), 0) FROM predictions").fetchone()[0]
        self.metrics = defaultdict(Metrics)
        self.restore_csvs()

    def close(self):
        if getattr(self, "db", None):
            self.db.close()
            self.db = None
        if getattr(self, "lock", None):
            self.lock.close()
            self.lock = None

    def keys(self, row):
        common = (row["route_id"], int(row["horizon_stops"]), int(row["bucket_minutes"]))
        return [("overall", *common, ""), ("hour", *common, row["reporting_hour"])]

    def restore_csvs(self):
        def forecasts():
            for record in self.db.execute("SELECT payload FROM predictions ORDER BY rowid"):
                row = json.loads(record[0])
                for key in self.keys(row):
                    self.metrics[key].add_created(row)
                yield {column: row.get(column, "") for column in PREDICTION_COLUMNS}
        atomic_csv(self.run_dir / "predictions.csv", PREDICTION_COLUMNS, forecasts())

        def results():
            common_group, resolved = None, []
            for record in self.db.execute("SELECT payload FROM predictions WHERE status!='pending' ORDER BY result_order"):
                row = json.loads(record[0])
                group = (row["origin_event_id"], row["horizon_stops"])
                if common_group != group and resolved:
                    self.add_common(resolved)
                    resolved = []
                common_group = group
                resolved.append(row)
                for key in self.keys(row):
                    self.metrics[key].add_result(row)
                yield {column: row.get(column, "") for column in RESULT_COLUMNS}
            self.add_common(resolved)
        atomic_csv(self.run_dir / "eval_results.csv", RESULT_COLUMNS, results())
        atomic_csv(self.run_dir / "arrivals.csv", ARRIVAL_COLUMNS,
                   (json.loads(r[0]) for r in self.db.execute("SELECT payload FROM arrivals ORDER BY rowid")))
        # SQLite is the durable raw response journal. Export it for easy replay
        # and inspection; a truncated text export never affects recovery.
        path = self.run_dir / "raw_observations.jsonl"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            for record in self.db.execute("SELECT payload FROM observations ORDER BY id"):
                output.write(record[0] + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        self.write_summary()

    def add_common(self, rows):
        if len(rows) == len(BUCKETS) and all(row["status"] == "resolved" for row in rows):
            for row in rows:
                for key in self.keys(row):
                    self.metrics[key].common_count += 1
                    self.metrics[key].common_total += float(row["absolute_error_seconds"])

    def append(self, filename, columns, rows):
        if not rows:
            return
        with (self.run_dir / filename).open("a", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
            writer.writerows(rows)
            output.flush()
            os.fsync(output.fileno())

    def finish(self, row, status, reason, at, actual=None, lower=""):
        row.update(status=status, reason=reason, finished_at=stamp(at))
        if actual is not None:
            row["actual_arrival"] = stamp(actual)
            row["actual_lower_bound"] = lower
            row["actual_elapsed_seconds"] = actual.timestamp() - parse_time(row["origin_arrival"]).timestamp()
            error = parse_time(row["predicted_arrival"]).timestamp() - actual.timestamp()
            row["error_seconds"], row["absolute_error_seconds"] = error, abs(error)
            if row["transloc_predicted_arrival"]:
                vendor_error = parse_time(row["transloc_predicted_arrival"]).timestamp() - actual.timestamp()
                row["transloc_error_seconds"] = vendor_error
                row["transloc_absolute_error_seconds"] = abs(vendor_error)
        self.result_order += 1
        self.db.execute("UPDATE predictions SET status=?, payload=?, result_order=? WHERE id=?",
                        (status, json.dumps(row), self.result_order, row["prediction_id"]))
        self.new_results.append(row)

    def invalidate(self, vid, reason, at):
        for record in self.db.execute("SELECT payload FROM predictions WHERE vehicle=? AND status='pending'", (vid,)).fetchall():
            self.finish(json.loads(record[0]), "excluded", reason, at)
        self.vehicles.pop(vid, None)

    def vendor(self, rid, vid, sid, at, full_circuit=False):
        entries = self.estimates.get(f"{rid}:{vid}:{sid}")
        if not entries:
            return "", "", "missing"
        # The API can return multiple circuits for the same vehicle/stop.
        # At the origin, the first estimate can still refer to the arrival
        # we just observed. Only compare a full circuit if that occurrence
        # is explicitly identifiable as IsArriving and a later one exists.
        if full_circuit:
            current = [entry for entry in entries if entry["is_arriving"]]
            if not current:
                return "", entries[0]["received"], "ambiguous_full_circuit"
            entries = [entry for entry in entries
                       if not entry["is_arriving"] and entry["arrival"] > max(e["arrival"] for e in current)]
            if not entries:
                return "", "", "missing_next_circuit"
        entry = entries[0]
        age = at.timestamp() - parse_time(entry["received"]).timestamp()
        if not 0 <= age <= self.config["max_estimate_age_seconds"]:
            return "", entry["received"], "stale"
        if entry["arrival"] < at.timestamp():
            return "", entry["received"], "past"
        return stamp(datetime.fromtimestamp(entry["arrival"], timezone.utc)), entry["received"], "available"

    def arrival(self, observation_id, vid, state, index, at, lower):
        rid = state["rid"]
        stops = self.routes[rid]["stops"]
        previous = state.get("last_index")
        if previous == index:
            return  # Geofence jitter cannot manufacture another circuit.
        if previous is not None and index != (previous + 1) % len(stops):
            self.invalidate(vid, "missing_stop_sequence", at)
            state["seq"] = 0
        else:
            state["seq"] = state.get("seq", -1) + 1
        state["last_index"] = index
        self.vehicles[vid] = state
        seq = state["seq"]
        event_id = f"{observation_id}:{vid}:{index}"
        event = dict(zip(ARRIVAL_COLUMNS, [
            event_id, rid, vid, state["name"], stops[index]["id"], index, seq,
            stamp(at), lower, state["source"],
        ]))
        self.db.execute("INSERT INTO arrivals VALUES (?, ?)", (event_id, json.dumps(event)))
        self.new_arrivals.append(event)

        pending = self.db.execute(
            "SELECT payload FROM predictions WHERE vehicle=? AND status='pending' AND target<=?", (vid, seq)).fetchall()
        resolved_groups = defaultdict(list)
        for record in pending:
            row = json.loads(record[0])
            if row["route_id"] == rid and row["target_sequence"] == seq and row["destination_route_stop_id"] == stops[index]["id"]:
                self.finish(row, "resolved", "", at, actual=at, lower=lower)
                resolved_groups[(row["origin_event_id"], row["horizon_stops"])].append(row)
            else:
                self.finish(row, "excluded", "ambiguous_destination", at)
        for rows in resolved_groups.values():
            self.add_common(rows)

        local = at.astimezone(LOCAL)
        for horizon in range(1, len(stops) + 1):
            sid = stops[(index + horizon) % len(stops)]["id"]
            vendor_at, vendor_received, vendor_status = self.vendor(rid, vid, sid, at, horizon == len(stops))
            for width in BUCKETS:
                item, fallback, bucket = self.model.lookup(rid, index, horizon, width, at)
                row = {column: "" for column in RESULT_COLUMNS}
                row.update(
                    prediction_id=f"{event_id}:{horizon}:{width}", origin_event_id=event_id,
                    route_id=rid, vehicle_id=vid, vehicle_name=state["name"],
                    origin_route_stop_id=stops[index]["id"], destination_route_stop_id=sid,
                    horizon_stops=horizon, bucket_minutes=width,
                    bucket_start_minute=bucket if width else "", origin_arrival=stamp(at),
                    predicted_arrival=stamp(datetime.fromtimestamp(at.timestamp() + item[0], timezone.utc)) if item else "",
                    mean_elapsed_seconds=item[0] if item else "", sample_count=item[1] if item else 0,
                    service_date_count=item[2] if item else 0, fallback_used=int(fallback),
                    transloc_predicted_arrival=vendor_at, transloc_received_at=vendor_received,
                    transloc_status=vendor_status, service_date=local.date().isoformat(),
                    reporting_hour=local.hour, model_hash=self.model.hash,
                    target_sequence=seq + horizon, status="pending",
                )
                self.db.execute("INSERT INTO predictions(id, vehicle, route, target, status, payload) VALUES (?, ?, ?, ?, ?, ?)", (
                    row["prediction_id"], vid, rid, seq + horizon, "pending", json.dumps(row)))
                self.new_predictions.append(row.copy())
                if item is None:
                    self.finish(row, "unavailable", "no_historical_average", at)

    def gps(self, observation_id, data, at):
        seen = set()
        for point in data:
            if not isinstance(point, dict) or point.get("VehicleID") is None:
                continue
            vid = str(point["VehicleID"])
            seen.add(vid)
            rid = str(point.get("RouteID", ""))
            state = self.vehicles.get(vid)
            if rid not in self.routes or point.get("IsOnRoute") is False:
                self.invalidate(vid, "off_route_or_unknown_route", at)
                continue
            try:
                lat, lon = float(point["Latitude"]), float(point["Longitude"])
                source = source_timestamp(point["TimeStamp"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("coordinates")
                if not math.isfinite(source):
                    raise ValueError("Invalid source timestamp")
            except (KeyError, ValueError, TypeError, OverflowError):
                self.invalidate(vid, "invalid_gps", at)
                continue
            if state and state["rid"] != rid:
                self.invalidate(vid, "route_changed", at)
                state = None
            if state and source <= state["source"]:
                if source < state["source"] or (lat, lon) != (state["lat"], state["lon"]):
                    self.invalidate(vid, "out_of_order_or_inconsistent_gps", at)
                continue  # Cached fixes never produce a second observation.
            if vid in self.stale_sources:
                previous = self.stale_sources[vid]
                if rid == previous["rid"] and source <= previous["source"]:
                    continue
                self.stale_sources.pop(vid)
            distances = [distance(lat, lon, stop["lat"], stop["lon"]) for stop in self.routes[rid]["stops"]]
            inside = [i for i, meters in enumerate(distances) if meters <= self.config["radius_meters"]]
            if len(inside) > 1:
                self.invalidate(vid, "ambiguous_geofence", at)
                continue
            if state is None:
                state = {"rid": rid, "seq": -1, "last_index": None, "inside": inside[0] if inside else None}
                lower = ""
                entered = False  # Startup-inside is not a bracketed arrival.
            else:
                lower = state["received"]
                entered = bool(inside and state["inside"] != inside[0])
            state.update(lat=lat, lon=lon, source=source, received=stamp(at),
                         name=str(point.get("Name", vid)), inside=inside[0] if inside else None)
            self.vehicles[vid] = state
            if entered:
                self.arrival(observation_id, vid, state, inside[0], at, lower)
        for vid in set(self.vehicles) - seen:
            self.invalidate(vid, "vehicle_missing", at)

    def arrival_estimates(self, data, at):
        # Each successful response replaces the cache. A disappeared estimate
        # must not remain available just because a previous response had it.
        estimates = {}
        for stop in data:
            if not isinstance(stop, dict):
                continue
            rid = str(stop.get("RouteID", stop.get("RouteId", "")))
            sid = str(stop.get("RouteStopID", stop.get("RouteStopId", "")))
            for entry in stop.get("Times", []) or []:
                if not isinstance(entry, dict):
                    continue
                vid = entry.get("VehicleId", entry.get("VehicleID"))
                if vid is None or entry.get("IsDeparted") is True:
                    continue
                try:
                    # EstimateTime is explicitly the estimate, whereas Time
                    # may be a displayed schedule/estimate combination.
                    if entry.get("EstimateTime"):
                        arrival = source_timestamp(entry["EstimateTime"])
                    else:
                        seconds = float(entry["Seconds"])
                        if not math.isfinite(seconds) or seconds < 0:
                            continue
                        arrival = at.timestamp() + seconds
                    if not math.isfinite(arrival) or arrival < at.timestamp():
                        continue
                except (KeyError, ValueError, TypeError, OverflowError):
                    continue
                key = f"{rid}:{vid}:{sid}"
                estimates.setdefault(key, []).append({"arrival": arrival, "received": stamp(at),
                                                       "is_arriving": entry.get("IsArriving") is True})
        for entries in estimates.values():
            entries.sort(key=lambda entry: entry["arrival"])
        self.estimates = estimates

    def process(self, observation):
        at = parse_time(observation["received_at"])
        if at <= self.model.training_end:
            raise ValueError("Evaluation observations must be later than the frozen training data")
        if self.last_at and at < parse_time(self.last_at):
            raise ValueError("Observation clock moved backwards; stop and inspect timestamps")
        self.new_predictions, self.new_results, self.new_arrivals = [], [], []
        with self.db:
            cursor = self.db.execute("INSERT INTO observations(kind, received_at, payload) VALUES (?, ?, ?)", (
                observation["kind"], stamp(at), json.dumps(observation)))
            # A gap invalidates active predictions even if the next received
            # observation is an estimate response rather than a GPS response.
            for vid, state in list(self.vehicles.items()):
                age = at.timestamp() - parse_time(state["received"]).timestamp()
                if age > self.config["max_observation_gap_seconds"]:
                    reason = "gps_observation_gap"
                elif age > self.config["max_gps_age_seconds"]:
                    reason = "stale_gps"
                else:
                    continue
                # Keep the expired ordering token across restart. Receiving an
                # identical cached fix again must not reset its freshness age.
                self.stale_sources[vid] = {"rid": state["rid"], "source": state["source"]}
                self.invalidate(vid, reason, at)
            if not observation.get("error"):
                data = observation.get("data")
                if not isinstance(data, list):
                    raise ValueError("Successful API observation must contain a JSON list")
                if observation["kind"] == "gps":
                    self.gps(cursor.lastrowid, data, at)
                elif observation["kind"] == "estimates":
                    self.arrival_estimates(data, at)
                elif observation["kind"] != "tick":
                    raise ValueError(f"Unknown observation kind {observation['kind']}")
            self.last_at = stamp(at)
            state = {"vehicles": self.vehicles, "stale_sources": self.stale_sources,
                     "estimates": self.estimates, "last_at": self.last_at}
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('state', ?)", (json.dumps(state),))
        for row in self.new_predictions:
            for key in self.keys(row):
                self.metrics[key].add_created(row)
        for row in self.new_results:
            for key in self.keys(row):
                self.metrics[key].add_result(row)
        self.append("predictions.csv", PREDICTION_COLUMNS, self.new_predictions)
        self.append("eval_results.csv", RESULT_COLUMNS, self.new_results)
        self.append("arrivals.csv", ARRIVAL_COLUMNS, self.new_arrivals)
        with (self.run_dir / "raw_observations.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(observation) + "\n")
            output.flush()
            os.fsync(output.fileno())
        if self.new_predictions or self.new_results:
            self.write_summary()
            logging.info("arrivals=%d forecasts=%d finalized=%d", len(self.new_arrivals),
                         len(self.new_predictions), len(self.new_results))

    def write_summary(self):
        at = self.last_at or stamp(utc_now())
        atomic_csv(self.run_dir / "eval_summary.csv", SUMMARY_COLUMNS,
                   (metrics.record(key, at) for key, metrics in sorted(self.metrics.items())))

    def write_selection(self):
        rows = []
        for rid, route in self.routes.items():
            supported = [h for h in range(1, len(route["stops"]) + 1)
                         if all(self.metrics[("overall", rid, h, width, "")].common_count for width in BUCKETS)]
            if not supported:
                continue
            candidate_rows = []
            for width in BUCKETS:
                metrics = [self.metrics[("overall", rid, h, width, "")] for h in supported]
                score = statistics.mean(m.common_total / m.common_count for m in metrics)
                dates = sorted(set().union(*(m.daily for m in metrics)))
                paired = sum(m.paired for m in metrics)
                improvement = sum(m.paired_vendor - m.paired_model for m in metrics) / paired if paired else ""
                samples = []
                if len(dates) >= 2:
                    rng = random.Random(0)
                    for _ in range(500):
                        chosen = rng.choices(dates, k=len(dates))
                        total, count = 0.0, 0
                        for date in chosen:
                            for metric in metrics:
                                model_sum, vendor_sum, n = metric.daily.get(date, (0, 0, 0))
                                total += vendor_sum - model_sum
                                count += n
                        if count:
                            samples.append(total / count)
                samples.sort()
                candidate_rows.append(dict(zip(SELECTION_COLUMNS, [
                    rid, width, ";".join(map(str, supported)),
                    ";".join(str(h) for h in range(1, len(route["stops"]) + 1) if h not in supported),
                    score, 0, improvement, quantile(samples, .025), quantile(samples, .975),
                    len(dates), "selection-set performance; not an independent test",
                ])))
            # Identical scores prefer no buckets, then the widest bucket.
            best = min(candidate_rows, key=lambda r: (r["equal_horizon_mae_seconds"],
                       0 if r["bucket_minutes"] == 0 else 1, -r["bucket_minutes"]))
            best["best_observed"] = 1
            rows.extend(candidate_rows)
        atomic_csv(self.run_dir / "bucket_comparison.csv", SELECTION_COLUMNS, rows)


def fetch(kind, base_url, timeout, stops):
    started = utc_now()
    method = "GetMapVehiclePoints" if kind == "gps" else "GetStopArrivalTimes"
    url = base_url.rstrip("/") + "/" + method
    if kind == "estimates":
        url += "?" + urllib.parse.urlencode({"routeStopIDs": ",".join(stops), "timesPerStop": 30})
    result = {"kind": kind, "started_at": stamp(started), "url": url, "data": None, "error": ""}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "gt-bus-eta-evaluation/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["status_code"] = response.status
            result["headers"] = dict(response.headers)
            result["data"] = json.load(response)
        if not isinstance(result["data"], list):
            raise ValueError("Expected a JSON list")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["received_at"] = stamp(utc_now())
    return result


def collect(evaluator, args):
    stop = threading.Event()
    previous_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, lambda *_: stop.set())
    intervals = {"gps": args.gps_seconds, "estimates": args.estimate_seconds}
    due, failures, outstanding = {kind: 0.0 for kind in intervals}, defaultdict(int), {}
    stops = sorted({s["id"] for route in evaluator.routes.values() for s in route["stops"]})
    started = time.monotonic()
    last_report = started
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            while not stop.is_set() and (args.duration_seconds is None or time.monotonic() - started < args.duration_seconds):
                now = time.monotonic()
                for kind in intervals:
                    if kind not in outstanding and now >= due[kind]:
                        outstanding[kind] = executor.submit(fetch, kind, args.base_url, args.timeout_seconds, stops)
                ready = [(kind, future.result()) for kind, future in outstanding.items() if future.done()]
                for kind, observation in sorted(ready, key=lambda pair: pair[1]["received_at"]):
                    evaluator.process(observation)
                    outstanding.pop(kind)
                    if observation["error"]:
                        failures[kind] += 1
                        logging.warning("%s: %s", kind, observation["error"])
                    else:
                        failures[kind] = 0
                    due[kind] = time.monotonic() + min(60, intervals[kind] * 2 ** min(failures[kind], 6))
                # Tick observations enforce gap exclusion during total API
                # failure, even if no successful GPS response ever follows.
                if not outstanding and not ready and evaluator.last_at and (utc_now() - parse_time(evaluator.last_at)).total_seconds() >= 1:
                    evaluator.process({"kind": "tick", "received_at": stamp(utc_now()), "data": [], "error": ""})
                if now - last_report >= 60:
                    evaluator.write_selection()
                    logging.info("Running; results are flushed under %s", evaluator.run_dir)
                    last_report = now
                stop.wait(.1)
            # A stop request prevents new work, but received in-flight
            # responses are committed before the final CSV refresh.
            for observation in sorted((f.result() for f in outstanding.values()), key=lambda row: row["received_at"]):
                if not evaluator.last_at or observation["received_at"] >= evaluator.last_at:
                    evaluator.process(observation)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        evaluator.write_summary()
        evaluator.write_selection()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--averages", type=Path, default=ROOT / "prediction/averages.csv")
    parser.add_argument("--routes", type=Path, default=ROOT / "reference/routes.json")
    parser.add_argument("--run-dir", type=Path, help="Existing directory resumes a run; default creates a new timestamped run")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--gps-seconds", type=float, default=3)
    parser.add_argument("--estimate-seconds", type=float, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--radius-meters", type=float, default=10)
    parser.add_argument("--max-gps-age-seconds", type=float, default=30)
    parser.add_argument("--max-observation-gap-seconds", type=float, default=30)
    parser.add_argument("--max-estimate-age-seconds", type=float, default=30)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--min-dates", type=int, default=3)
    parser.add_argument("--duration-seconds", type=float, help="Optional bounded run; otherwise stop with Ctrl+C")
    parser.add_argument("--replay", type=Path, help="Replay a raw_observations.jsonl into a NEW run directory without API requests")
    args = parser.parse_args()
    for name in ("gps_seconds", "estimate_seconds", "timeout_seconds", "radius_meters", "max_gps_age_seconds",
                 "max_observation_gap_seconds", "max_estimate_age_seconds", "min_samples", "min_dates", "duration_seconds"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"--{name.replace('_', '-')} must be positive and finite")
    run_dir = args.run_dir or ROOT / "data/prediction_eval" / utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    if args.replay and (run_dir / "checkpoint.sqlite3").exists():
        parser.error("Replay requires a new run directory")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    evaluator = None
    try:
        model = Model(args.averages, load_routes(args.routes), args.min_samples, args.min_dates)
        config = {key: getattr(args, key) for key in (
            "radius_meters", "max_gps_age_seconds", "max_observation_gap_seconds", "max_estimate_age_seconds")}
        config["base_url"] = args.base_url
        evaluator = Evaluator(run_dir, model, args.routes, config)
        logging.info("Frozen model %s; CSV outputs: %s", model.hash[:12], run_dir)
        if args.replay:
            with args.replay.open(encoding="utf-8") as source:
                for line in source:
                    evaluator.process(json.loads(line))
            evaluator.write_summary()
            evaluator.write_selection()
        else:
            collect(evaluator, args)
    except (ValueError, OSError, sqlite3.Error) as exc:
        logging.error("%s", exc)
        return 1
    finally:
        if evaluator is not None:
            evaluator.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
