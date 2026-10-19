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
import multiprocessing
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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from .build_predictions import BUCKETS, LOCAL, ROOT, atomic_csv, load_routes, parse_time
except ImportError:
    from build_predictions import BUCKETS, LOCAL, ROOT, atomic_csv, load_routes, parse_time

BASE_URL = "https://bus.gatech.edu/Services/JSONPRelay.svc"
SOURCE_TIME = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")
PREDICTION_COLUMNS = [
    "prediction_id", "origin_event_id", "route_id", "route_color", "vehicle_id", "vehicle_name",
    "origin_route_stop_id", "destination_route_stop_id", "horizon_stops",
    "bucket_minutes", "bucket_start_minute", "origin_arrival", "predicted_arrival",
    "mean_elapsed_seconds", "sample_count", "service_date_count", "fallback_used",
    "transloc_predicted_arrival", "transloc_received_at", "transloc_status",
    "service_date", "reporting_hour", "model_hash",
    "origin_sequence",
]
RESULT_COLUMNS = PREDICTION_COLUMNS + [
    "status", "reason", "actual_arrival", "actual_lower_bound", "actual_elapsed_seconds",
    "error_seconds", "absolute_error_seconds", "transloc_error_seconds",
    "transloc_absolute_error_seconds", "target_sequence", "finished_at",
    "intermediate_observations_missing", "missed_intermediate_count",
]
ARRIVAL_COLUMNS = [
    "event_id", "route_id", "vehicle_id", "vehicle_name", "route_stop_id",
    "stop_index", "sequence", "observed_arrival", "lower_bound", "source_timestamp",
    "route_progress_m", "circuit",
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
    "complete_observations_count", "complete_observations_mae_seconds",
    "missing_intermediate_count", "missing_intermediate_mae_seconds",
    "route_color",
]
DETECTOR_COLUMNS = ["observation_id", "observed_at", "vehicle_id", "route_id", "event",
                    "route_stop_id", "progress_m", "circuit", "distance_m", "detail"]
SELECTION_COLUMNS = [
    "route_id", "bucket_minutes", "supported_horizons", "unsupported_horizons",
    "equal_horizon_mae_seconds", "best_observed", "paired_improvement_seconds",
    "paired_improvement_ci_low", "paired_improvement_ci_high", "paired_service_dates",
    "performance_label",
    "route_color",
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


def decode_polyline(encoded):
    points, cursor, lat, lon = [], 0, 0, 0
    while cursor < len(encoded):
        delta = []
        for _ in range(2):
            value = shift = 0
            while True:
                if cursor >= len(encoded) or shift > 30:
                    raise ValueError("Malformed route polyline")
                byte = ord(encoded[cursor]) - 63
                cursor += 1
                if not 0 <= byte <= 63:
                    raise ValueError("Malformed route polyline")
                value |= (byte & 31) << shift
                shift += 5
                if byte < 32:
                    break
            delta.append(~(value >> 1) if value & 1 else value >> 1)
        lat += delta[0]
        lon += delta[1]
        points.append((lat / 100000, lon / 100000))
    return points


class RouteGeometry:
    """Ordered stop anchors and a small causal beam of route-position hypotheses.

    A position is unwrapped meters along the directed circuit. Crossing shared
    pavement does not change branches without a plausible forward transition.
    Defaults (35m/s maximum progress, 15m backwards GPS jitter, 8m movement for
    direction) are physical matching bounds, not fitted from prediction errors.
    """
    def __init__(self, stops, points, corridor=40):
        if len(points) < 3:
            raise ValueError("Missing or incomplete route polyline")
        self.scale = 111320 * math.cos(math.radians(points[0][0]))
        self.corridor = corridor
        self.segments, self.length = [], 0.0
        xy = [self.xy(lat, lon) for lat, lon in points]
        if math.dist(xy[0], xy[-1]) > 25:
            raise ValueError("Route polyline is not a closed circuit within 25m")
        if xy[-1] != xy[0]:
            xy.append(xy[0])
        for (x, y), (xx, yy) in zip(xy, xy[1:]):
            length = math.hypot(xx - x, yy - y)
            if length:
                self.segments.append((x, y, xx - x, yy - y, self.length, length))
                self.length += length
        if self.length <= 0:
            raise ValueError("Route polyline has no length")
        self.grid = defaultdict(list)
        padding = max(25, corridor)
        for segment in self.segments:
            a, b, dx, dy, _, _ = segment
            for gx in range(math.floor((min(a, a + dx) - padding) / 100), math.floor((max(a, a + dx) + padding) / 100) + 1):
                for gy in range(math.floor((min(b, b + dy) - padding) / 100), math.floor((max(b, b + dy) + padding) / 100) + 1):
                    self.grid[gx, gy].append(segment)
        candidates = [self.project(s["lat"], s["lon"], 25, rotate=False) for s in stops]
        if any(not values for values in candidates):
            raise ValueError("A stop has no path anchor within 25m")
        best = None
        # Jointly choose all anchors in one directed circuit. Greedy nearest
        # points can put a return stop on an overlapping outbound segment.
        for start in candidates[0]:
            states = [(start[0], start[1] ** 2, [start[0]])]
            for options in candidates[1:]:
                next_states = []
                for pos, error, _ in options:
                    for unwrapped in (pos, pos + self.length):
                        if not start[0] + .1 < unwrapped < start[0] + self.length - .1:
                            continue
                        allowed = [(cost + error ** 2, path + [unwrapped])
                                   for previous, cost, path in states if previous + .1 < unwrapped]
                        if allowed:
                            cost, path = min(allowed)
                            next_states.append((unwrapped, cost, path))
                states = next_states
                if not states:
                    break
            if states:
                candidate = min(states, key=lambda state: state[1])
                if best is None or candidate[1] < best[1]:
                    best = candidate
        if best is None:
            raise ValueError("Stops cannot be anchored in order within one circuit")
        self.start = best[2][0]
        self.anchors = [pos - self.start for pos in best[2]]

    def xy(self, lat, lon):
        return lon * self.scale, lat * 111320

    def project(self, lat, lon, radius=None, rotate=True):
        x, y = self.xy(lat, lon)
        candidates = []
        for a, b, dx, dy, offset, length in self.grid.get((math.floor(x / 100), math.floor(y / 100)), []):
            fraction = max(0, min(1, ((x - a) * dx + (y - b) * dy) / length ** 2))
            error = math.hypot(x - a - fraction * dx, y - b - fraction * dy)
            if error <= (self.corridor if radius is None else radius):
                pos = offset + fraction * length
                if rotate:
                    pos = (pos - self.start) % self.length
                candidates.append((pos, error, (dx / length, dy / length)))
        return candidates

    def advance(self, state, lat, lon, at):
        candidates = self.project(lat, lon)
        if not candidates:
            return None
        previous = state.get("paths", [])
        x, y = self.xy(lat, lon)
        old_x, old_y = self.xy(state.get("lat", lat), state.get("lon", lon))
        dx, dy = x - old_x, y - old_y
        movement = math.hypot(dx, dy)
        dt = at.timestamp() - parse_time(state["received"]).timestamp() if state.get("received") else 0
        possibilities = []
        initial_reference = min(candidates, key=lambda candidate: candidate[1])[0]
        for pos, error, tangent in candidates:
            alignment = (dx * tangent[0] + dy * tangent[1]) / movement if movement >= 8 else 1
            if alignment < -.25:
                continue
            if not previous:
                # Startup has no absolute circuit identity. Projections either
                # side of the loop seam are equivalent hypotheses, not two
                # permanently ambiguous paths separated by a full circuit.
                pos += round((initial_reference - pos) / self.length) * self.length
                possibilities.append({"p": pos, "score": error, "moved": False})
            else:
                for path in previous:
                    lap = math.floor((path["p"] - pos) / self.length)
                    for circuit in (lap, lap + 1):
                        progress = pos + circuit * self.length
                        change = progress - path["p"]
                        if -15 <= change <= max(20, 35 * dt + 20):
                            possibilities.append({
                                "p": max(path["p"], progress),
                                "score": .8 * path["score"] + error + 8 * (1 - alignment)
                                         + .15 * abs(max(0, change) - movement),
                                "moved": path["moved"] or movement >= 8,
                            })
        if not possibilities:
            return None
        possibilities.sort(key=lambda path: path["score"])
        paths = []
        for path in possibilities:
            if path["score"] > possibilities[0]["score"] + 12:
                break
            if all(abs(path["p"] - other["p"]) > 8 for other in paths):
                paths.append(path)
            if len(paths) == 8:
                break
        confident = all(path["moved"] for path in paths) and max(p["p"] for p in paths) - min(p["p"] for p in paths) <= 25
        return paths, paths[0]["p"], confident

    def occurrence(self, index, progress):
        lap = round((progress - self.anchors[index]) / self.length)
        return lap * len(self.anchors) + index, lap * self.length + self.anchors[index]

    def target_progress(self, sequence):
        circuit, index = divmod(sequence, len(self.anchors))
        return circuit * self.length + self.anchors[index]


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
        self.complete_count = self.missing_count = 0
        self.complete_error = self.missing_error = 0.0
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
            if int(row.get("intermediate_observations_missing") or 0):
                self.missing_count += 1
                self.missing_error += abs(error)
            else:
                self.complete_count += 1
                self.complete_error += abs(error)
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
            self.complete_count, self.complete_error / self.complete_count if self.complete_count else "",
            self.missing_count, self.missing_error / self.missing_count if self.missing_count else "",
            "",
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
        self.config = {"exit_radius_meters": 25, "route_corridor_meters": 40, **config}
        if self.config["exit_radius_meters"] <= self.config["radius_meters"]:
            self.close()
            raise ValueError("Exit radius must exceed entry radius")
        raw_routes = {str(route["RouteID"]): route for route in json.loads(Path(routes_path).read_text())}
        self.geometry, self.geometry_errors = {}, {}
        for rid, route in self.routes.items():
            try:
                self.geometry[rid] = RouteGeometry(route["stops"],
                    decode_polyline(raw_routes.get(rid, {}).get("EncodedPolyline", "")),
                    self.config["route_corridor_meters"])
            except ValueError as exc:
                self.geometry_errors[rid] = str(exc)
                logging.error("Unusable geometry for route %s: %s", rid, exc)
        geometry_hash = hashlib.sha256(json.dumps({rid: raw_routes.get(rid, {}).get("EncodedPolyline", "")
                          for rid in self.routes}, sort_keys=True).encode()).hexdigest()
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
        frozen = {"model_hash": model.hash, "routes_hash": digest(routes_path), "config": self.config,
                  "geometry_hash": geometry_hash, "min_samples": model.min_samples,
                  "min_dates": model.min_dates, "format_version": 4}
        existing = self.db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()
        if existing and json.loads(existing[0]) != frozen:
            self.close()
            raise ValueError("Run settings/model or detector policy changed; use a new --run-dir. "
                             "Replay the raw journal to recover results with the corrected policy.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('frozen', ?)", (json.dumps(frozen),))
            self.db.execute("CREATE TABLE IF NOT EXISTS detector_events (id INTEGER PRIMARY KEY, payload TEXT)")
        state = self.db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
        saved = json.loads(state[0]) if state else {}
        self.vehicles = saved.get("vehicles", {})
        self.stale_sources = saved.get("stale_sources", {})
        self.estimates = saved.get("estimates", {})
        self.last_at = saved.get("last_at", "")
        self.diagnostics = Counter(saved.get("diagnostics", {}))
        for rid in self.geometry_errors:
            self.diagnostics[f"route_geometry_unusable:{rid}"] = 1
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

    def route_labeled(self, row):
        # Also enrich old checkpoint rows during CSV recovery. No detector
        # version change is needed for this presentation-only column.
        return {**row, "route_color": self.routes.get(str(row["route_id"]), {}).get("name", str(row["route_id"]))}

    def restore_csvs(self):
        def forecasts():
            for record in self.db.execute("SELECT payload FROM predictions ORDER BY rowid"):
                row = self.route_labeled(json.loads(record[0]))
                for key in self.keys(row):
                    self.metrics[key].add_created(row)
                yield {column: row.get(column, "") for column in PREDICTION_COLUMNS}
        atomic_csv(self.run_dir / "predictions.csv", PREDICTION_COLUMNS, forecasts())

        def results():
            common_group, resolved = None, []
            for record in self.db.execute("SELECT payload FROM predictions WHERE status!='pending' ORDER BY result_order"):
                row = self.route_labeled(json.loads(record[0]))
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
        atomic_csv(self.run_dir / "detector_events.csv", DETECTOR_COLUMNS,
                   (json.loads(r[0]) for r in self.db.execute("SELECT payload FROM detector_events ORDER BY id")))
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
        row.update(self.route_labeled(row))
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
        self.diagnostics[f"{status}_forecasts" + (f":{reason}" if reason else "")] += 1

    def detector_event(self, vid, rid, event, at, sid="", progress="", meters="", detail=""):
        row = dict(zip(DETECTOR_COLUMNS, [self.observation_id, stamp(at), vid, rid, event, sid,
                   progress, math.floor(progress / self.geometry[rid].length) if progress != "" and rid in self.geometry else "",
                   meters, detail]))
        self.db.execute("INSERT INTO detector_events(payload) VALUES (?)", (json.dumps(row),))
        self.new_detector_events.append(row)

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

    def arrival(self, observation_id, vid, state, index, at, lower, sequence):
        rid = state["rid"]
        stops = self.routes[rid]["stops"]
        if state.get("last_arrival_sequence") is not None and sequence <= state["last_arrival_sequence"]:
            return
        state["seq"] = sequence
        state["last_arrival_sequence"] = sequence
        state["last_index"] = index
        state["observed_sequences"] = [s for s in state.get("observed_sequences", []) if s >= sequence - len(stops)] + [sequence]
        self.vehicles[vid] = state
        seq = state["seq"]
        event_id = f"{observation_id}:{vid}:{index}"
        event = dict(zip(ARRIVAL_COLUMNS, [
            event_id, rid, vid, state["name"], stops[index]["id"], index, seq,
            stamp(at), lower, state["source"],
            state["progress"], sequence // len(stops),
        ]))
        self.db.execute("INSERT INTO arrivals VALUES (?, ?)", (event_id, json.dumps(event)))
        self.new_arrivals.append(event)
        self.diagnostics["observed_arrivals"] += 1
        self.detector_event(vid, rid, "arrival", at, stops[index]["id"], state["progress"],
                            detail=f"sequence={sequence}")

        pending = self.db.execute(
            "SELECT payload FROM predictions WHERE vehicle=? AND status='pending' AND target<=?", (vid, seq)).fetchall()
        resolved_groups = defaultdict(list)
        for record in pending:
            row = json.loads(record[0])
            if row["route_id"] == rid and row["target_sequence"] == seq and row["destination_route_stop_id"] == stops[index]["id"]:
                observed = sum(row["origin_sequence"] < s < seq for s in state["observed_sequences"])
                missed = max(0, int(row["horizon_stops"]) - 1 - observed)
                row.update(intermediate_observations_missing=int(missed > 0), missed_intermediate_count=missed)
                self.finish(row, "resolved", "", at, actual=at, lower=lower)
                resolved_groups[(row["origin_event_id"], row["horizon_stops"])].append(row)
            else:
                self.finish(row, "excluded", "destination_not_observed", at)
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
                    route_id=rid, route_color=self.routes[rid].get("name", rid), vehicle_id=vid, vehicle_name=state["name"],
                    origin_route_stop_id=stops[index]["id"], destination_route_stop_id=sid,
                    horizon_stops=horizon, bucket_minutes=width,
                    bucket_start_minute=bucket if width else "", origin_arrival=stamp(at),
                    predicted_arrival=stamp(datetime.fromtimestamp(at.timestamp() + item[0], timezone.utc)) if item else "",
                    mean_elapsed_seconds=item[0] if item else "", sample_count=item[1] if item else 0,
                    service_date_count=item[2] if item else 0, fallback_used=int(fallback),
                    transloc_predicted_arrival=vendor_at, transloc_received_at=vendor_received,
                    transloc_status=vendor_status, service_date=local.date().isoformat(),
                    reporting_hour=local.hour, model_hash=self.model.hash,
                    target_sequence=seq + horizon, origin_sequence=seq, status="pending",
                    intermediate_observations_missing=0, missed_intermediate_count=0,
                )
                self.db.execute("INSERT INTO predictions(id, vehicle, route, target, status, payload) VALUES (?, ?, ?, ?, ?, ?)", (
                    row["prediction_id"], vid, rid, seq + horizon, "pending", json.dumps(row)))
                self.new_predictions.append(row.copy())
                if item is None:
                    self.finish(row, "unavailable", "no_historical_average", at)

    def expire_passed_destinations(self, vid, state, at):
        geometry = self.geometry[state["rid"]]
        tolerance = 25 + self.config["radius_meters"]
        pending = self.db.execute("SELECT payload FROM predictions WHERE vehicle=? AND status='pending'", (vid,)).fetchall()
        missed = set()
        for record in pending:
            row = json.loads(record[0])
            if state["progress"] > geometry.target_progress(row["target_sequence"]) + tolerance:
                self.finish(row, "excluded", "destination_not_observed", at)
                missed.add(row["target_sequence"])
        for sequence in sorted(missed):
            self.detector_event(vid, state["rid"], "destination_passed_unobserved", at,
                                self.routes[state["rid"]]["stops"][sequence % len(geometry.anchors)]["id"],
                                state["progress"], detail=f"sequence={sequence}")

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
            if rid not in self.geometry:
                self.diagnostics["fixes_without_usable_geometry"] += 1
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
                    self.diagnostics["invalid_order_fixes"] += 1
                else:
                    self.diagnostics["duplicate_fixes"] += 1
                continue  # Cached fixes never produce a second observation.
            if vid in self.stale_sources:
                previous = self.stale_sources[vid]
                if rid == previous["rid"] and source <= previous["source"]:
                    continue
                self.stale_sources.pop(vid)
            self.diagnostics["fresh_fixes"] += 1
            geometry = self.geometry[rid]
            previous = state or {}
            match = geometry.advance(previous, lat, lon, at)
            if match is None:
                self.diagnostics["unmatched_fixes"] += 1
                self.detector_event(vid, rid, "unmatched_fix", at)
                self.invalidate(vid, "unmatched_route_progress", at)
                continue
            paths, progress, confident = match
            state = dict(previous)
            lower = state.get("received", "")
            prior_inside = state.get("physical_inside", [])
            if state.get("locked") and not confident:
                self.invalidate(vid, "ambiguous_route_progress", at)
                state.pop("last_arrival_sequence", None)
                state["observed_sequences"] = []
            state.update(rid=rid, paths=paths, progress=progress, locked=confident,
                         lat=lat, lon=lon, source=source, received=stamp(at),
                         name=str(point.get("Name", vid)))
            distances = [distance(lat, lon, stop["lat"], stop["lon"]) for stop in self.routes[rid]["stops"]]
            inside = [i for i, meters in enumerate(distances) if meters <= self.config["radius_meters"]]
            latch = state.get("latch")
            if latch is not None and distances[latch] > self.config["exit_radius_meters"]:
                state["latch"] = None
            plausible = []
            for index in inside:
                sequence, anchor = geometry.occurrence(index, progress)
                if abs(anchor - progress) <= 25 + self.config["radius_meters"]:
                    plausible.append((index, sequence))
                elif confident:
                    self.diagnostics["rejected_return_direction_candidates"] += 1
                    self.detector_event(vid, rid, "rejected_other_route_portion", at,
                                        self.routes[rid]["stops"][index]["id"], progress, distances[index])
            state["physical_inside"] = inside
            self.vehicles[vid] = state
            if not confident or len(plausible) > 1:
                self.diagnostics["ambiguous_matches"] += 1
                if inside:
                    self.detector_event(vid, rid, "ambiguous_match", at, progress=progress)
                if len(plausible) > 1:
                    self.invalidate(vid, "ambiguous_geofence", at)
                continue
            if len(plausible) == 1:
                index, sequence = plausible[0]
                if lower and index not in prior_inside and state.get("latch") != index:
                    state["latch"] = index
                    self.arrival(observation_id, vid, state, index, at, lower, sequence)
            self.expire_passed_destinations(vid, state, at)
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
        self.new_predictions, self.new_results, self.new_arrivals, self.new_detector_events = [], [], [], []
        with self.db:
            cursor = self.db.execute("INSERT INTO observations(kind, received_at, payload) VALUES (?, ?, ?)", (
                observation["kind"], stamp(at), json.dumps(observation)))
            self.observation_id = cursor.lastrowid
            self.diagnostics[f"observations:{observation['kind']}"] += 1
            if observation.get("error"):
                self.diagnostics[f"request_errors:{observation['kind']}"] += 1
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
                     "estimates": self.estimates, "last_at": self.last_at,
                     "diagnostics": dict(self.diagnostics)}
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
        self.append("detector_events.csv", DETECTOR_COLUMNS, self.new_detector_events)
        with (self.run_dir / "raw_observations.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(observation) + "\n")
            output.flush()
            os.fsync(output.fileno())
        if self.new_predictions or self.new_results:
            self.write_summary()
            logging.info("arrivals=%d forecasts=%d finalized=%d", len(self.new_arrivals),
                         len(self.new_predictions), len(self.new_results))
        elif observation["kind"] == "gps" or observation.get("error"):
            self.write_diagnostics()

    def write_summary(self):
        at = self.last_at or stamp(utc_now())
        atomic_csv(self.run_dir / "eval_summary.csv", SUMMARY_COLUMNS,
                   (self.route_labeled(metrics.record(key, at)) for key, metrics in sorted(self.metrics.items())))
        self.write_diagnostics()

    def write_diagnostics(self):
        atomic_csv(self.run_dir / "diagnostics.csv", ["metric", "count", "updated_at"],
                   ({"metric": key, "count": count, "updated_at": self.last_at}
                    for key, count in sorted(self.diagnostics.items())))

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
                    route.get("name", rid),
                ])))
            # Identical scores prefer no buckets, then the widest bucket.
            best = min(candidate_rows, key=lambda r: (r["equal_horizon_mae_seconds"],
                       0 if r["bucket_minutes"] == 0 else 1, -r["bucket_minutes"]))
            best["best_observed"] = 1
            rows.extend(candidate_rows)
        atomic_csv(self.run_dir / "bucket_comparison.csv", SELECTION_COLUMNS, rows)


def fetch(kind, base_url, timeout, stops):
    started = utc_now()
    monotonic_started = time.monotonic()
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
    result["duration_seconds"] = time.monotonic() - monotonic_started
    return result


def request_worker(connection):
    """Only this killable process performs DNS, TLS, and blocking HTTP reads."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        while True:
            request = connection.recv()
            if request is None:
                break
            connection.send(fetch(*request))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


class EndpointWorker:
    def __init__(self, kind, base_url, timeout, stops, target=request_worker):
        self.kind, self.base_url, self.timeout, self.stops = kind, base_url, timeout, stops
        self.target = target
        self.process = self.connection = None
        self.deadline = None

    def start(self):
        if self.process is None or not self.process.is_alive():
            self.close()
            context = multiprocessing.get_context("spawn")
            self.connection, child = context.Pipe()
            self.process = context.Process(target=self.target, args=(child,), daemon=True)
            self.process.start()
            child.close()
        self.started_at = stamp(utc_now())
        self.deadline = time.monotonic() + self.timeout
        self.connection.send((self.kind, self.base_url, self.timeout, self.stops))

    def poll(self):
        if self.deadline is None:
            return None
        response = None
        try:
            if self.connection.poll():
                response = self.connection.recv()
                if response.get("duration_seconds", 0) > self.timeout:
                    response = None
                    self.deadline = 0
            elif self.process.is_alive() and time.monotonic() < self.deadline:
                return None
        except (EOFError, OSError):
            pass
        if response is None:
            self.close()
            response = {"kind": self.kind, "started_at": self.started_at, "data": None,
                        "error": "Total request deadline exceeded or worker exited", "received_at": stamp(utc_now())}
        # Preserve network completion time as evidence, but serialize local
        # observation receipts in the parent alongside independent gap ticks.
        response["response_completed_at"] = response["received_at"]
        response["received_at"] = stamp(utc_now())
        self.deadline = None
        return response

    def close(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=.5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=.5)
            self.process.close()
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.deadline = None


def collect(evaluator, args):
    stop = threading.Event()
    previous_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, lambda *_: stop.set())
    intervals = {"gps": args.gps_seconds, "estimates": args.estimate_seconds}
    due, failures = {kind: 0.0 for kind in intervals}, defaultdict(int)
    stops = sorted({s["id"] for route in evaluator.routes.values() for s in route["stops"]})
    started = time.monotonic()
    last_report = started
    workers = {kind: EndpointWorker(kind, args.base_url, args.timeout_seconds, stops) for kind in intervals}
    pid_path = evaluator.run_dir / "collector.pid"
    pid_path.write_text(str(os.getpid()))
    try:
        draining = False
        while True:
            now = time.monotonic()
            draining = draining or stop.is_set() or (args.duration_seconds is not None and now - started >= args.duration_seconds)
            for kind, worker in workers.items():
                observation = worker.poll()
                if observation is not None:
                    evaluator.process(observation)
                    failures[kind] = failures[kind] + 1 if observation["error"] else 0
                    if observation["error"]:
                        logging.warning("%s: %s", kind, observation["error"])
                    due[kind] = time.monotonic() + min(60, intervals[kind] * 2 ** min(failures[kind], 6))
                if not draining and worker.deadline is None and now >= due[kind]:
                    worker.start()
            if evaluator.last_at and (utc_now() - parse_time(evaluator.last_at)).total_seconds() >= 1:
                evaluator.process({"kind": "tick", "received_at": stamp(utc_now()), "data": [], "error": ""})
            if now - last_report >= 60:
                evaluator.write_selection()
                logging.info("Running; results are flushed under %s", evaluator.run_dir)
                last_report = now
            if draining and all(worker.deadline is None for worker in workers.values()):
                break
            time.sleep(.05)
    finally:
        for worker in workers.values():
            worker.close()
        pid_path.unlink(missing_ok=True)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        evaluator.write_summary()
        evaluator.write_selection()


def replay(evaluator, path):
    """A SQLite read transaction pins a consistent snapshot of an active run."""
    path = Path(path).resolve()
    count = 0
    if path.suffix in (".sqlite3", ".sqlite", ".db"):
        source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            source.execute("BEGIN")
            previous = json.loads(source.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()[0])
            if previous["model_hash"] != evaluator.model.hash:
                raise ValueError("Replay requires the source run's frozen historical averages")
            cutoff = source.execute("SELECT COALESCE(MAX(id), 0) FROM observations").fetchone()[0]
            for record in source.execute("SELECT payload FROM observations WHERE id<=? ORDER BY id", (cutoff,)):
                evaluator.process(json.loads(record[0]))
                count += 1
                if count % 500 == 0:
                    logging.info("Replayed %d observations from snapshot ending at %d", count, cutoff)
            manifest = {"source": str(path), "source_observation_cutoff": cutoff,
                        "source_detector_version": previous.get("format_version"), "observations": count}
        finally:
            source.close()
    else:
        with path.open(encoding="utf-8") as source:
            # Pin the byte boundary before reading an actively appended journal.
            boundary = os.fstat(source.fileno()).st_size
            while source.tell() < boundary:
                line = source.readline()
                if source.tell() > boundary or not line.endswith("\n"):
                    break
                evaluator.process(json.loads(line))
                count += 1
        manifest = {"source": str(path), "source_byte_cutoff": boundary, "observations": count}
    manifest.update(detector_version=4, model_hash=evaluator.model.hash, completed_at=stamp(utc_now()))
    (evaluator.run_dir / "replay_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    evaluator.write_summary()
    evaluator.write_selection()
    return count


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
    parser.add_argument("--exit-radius-meters", type=float, default=25)
    parser.add_argument("--route-corridor-meters", type=float, default=40)
    parser.add_argument("--max-gps-age-seconds", type=float, default=30)
    parser.add_argument("--max-observation-gap-seconds", type=float, default=30)
    parser.add_argument("--max-estimate-age-seconds", type=float, default=30)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--min-dates", type=int, default=3)
    parser.add_argument("--duration-seconds", type=float, help="Optional bounded run; otherwise stop with Ctrl+C")
    parser.add_argument("--replay", type=Path, help="Replay a checkpoint.sqlite3 snapshot or raw journal into a NEW run directory")
    args = parser.parse_args()
    for name in ("gps_seconds", "estimate_seconds", "timeout_seconds", "radius_meters", "exit_radius_meters", "route_corridor_meters", "max_gps_age_seconds",
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
            "radius_meters", "exit_radius_meters", "route_corridor_meters", "max_gps_age_seconds", "max_observation_gap_seconds", "max_estimate_age_seconds")}
        config["base_url"] = args.base_url
        evaluator = Evaluator(run_dir, model, args.routes, config)
        logging.info("Frozen model %s; CSV outputs: %s", model.hash[:12], run_dir)
        if args.replay:
            replay(evaluator, args.replay)
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
