"""Regression checks for the GT GPS clock, independent of live API access."""

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from prediction.eval_loop import BUCKETS, Evaluator, EndpointWorker, RouteGeometry, collect, decode_polyline, load_routes, source_timestamp, stamp


def hung_request_worker(connection):
    import time
    connection.recv()
    time.sleep(60)


def encode_polyline(points):
    result, previous = [], (0, 0)
    for lat, lon in points:
        current = (round(lat * 100000), round(lon * 100000))
        for value, old in zip(current, previous):
            delta = value - old
            encoded = ~(delta << 1) if delta < 0 else delta << 1
            while encoded >= 32:
                result.append(chr((32 | (encoded & 31)) + 63))
                encoded >>= 5
            result.append(chr(encoded + 63))
        previous = current
    return "".join(result)


class GPSClockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.routes_path = self.root / "routes.json"
        self.routes_path.write_text(json.dumps([{"RouteID": 20, "EncodedPolyline": encode_polyline([
            (33, -84), (33.001, -84), (33.002, -84), (33.002, -83.999),
            (32.999, -83.999), (32.999, -84), (33, -84),
        ])}]))
        self.at = datetime(2026, 10, 19, 14, 54, 19, 137328, tzinfo=timezone.utc)
        routes = {"20": {"stops": [
            {"id": str(10 + i), "lat": 33 + i * .001, "lon": -84}
            for i in range(3)
        ]}}
        self.model = SimpleNamespace(
            hash="test-frozen-model", routes=routes, min_samples=20, min_dates=3,
            training_end=self.at - timedelta(days=1),
            lookup=lambda rid, origin, horizon, width, at: ((horizon * 60, 100, 4), False, 0),
        )
        self.config = {"radius_meters": 10, "max_gps_age_seconds": 30,
                       "max_observation_gap_seconds": 30, "max_estimate_age_seconds": 30,
                       "base_url": "https://example.invalid"}
        self.engine = None

    def tearDown(self):
        if self.engine:
            self.engine.close()
        self.temporary.cleanup()

    def open_engine(self):
        if self.engine:
            self.engine.close()
        self.engine = Evaluator(self.root / "run", self.model, self.routes_path, self.config)

    def gps(self, seconds, index=None, token_seconds=None, **changes):
        at = self.at + timedelta(seconds=seconds)
        source = self.at + timedelta(seconds=(seconds if token_seconds is None else token_seconds) + 21615)
        point = {"VehicleID": 5, "Name": "2204", "RouteID": 20, "IsOnRoute": True,
                 "Latitude": 33 + index * .001 if index is not None else (32.9995 if seconds == 0 else 33.0005),
                 "Longitude": -84, "Seconds": 17,
                 "TimeStamp": f"/Date({int(source.timestamp() * 1000)}-0600)/"}
        point.update(changes)
        self.engine.process({"kind": "gps", "received_at": stamp(at), "data": [point], "error": ""})

    def tick(self, seconds):
        self.engine.process({"kind": "tick", "received_at": stamp(self.at + timedelta(seconds=seconds)),
                             "data": [], "error": ""})

    def rows(self, filename):
        with (self.root / "run" / filename).open(newline="") as source:
            return list(csv.DictReader(source))

    def test_offset_gps_produces_arrivals_forecasts_and_correct_errors(self):
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        self.assertEqual(len(self.rows("predictions.csv")), 3 * len(BUCKETS))
        self.gps(4)
        self.gps(6, 1)
        results = self.rows("eval_results.csv")
        self.assertEqual(len(results), len(BUCKETS))
        self.assertTrue(all(r["status"] == "resolved" for r in results))
        self.assertTrue(all(float(r["actual_elapsed_seconds"]) == 4 for r in results))
        self.assertTrue(all(float(r["error_seconds"]) == 56 for r in results))

    def test_countdown_seconds_and_cached_fix_do_not_refresh_observation(self):
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        self.gps(5, 0, token_seconds=2, Seconds=14)
        self.assertEqual(self.engine.vehicles["5"]["received"], stamp(self.at + timedelta(seconds=2)))
        self.assertEqual(len(self.rows("arrivals.csv")), 1)

    def test_gps_outage_still_excludes_forecasts(self):
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        self.gps(35, 1)
        results = self.rows("eval_results.csv")
        self.assertEqual(len(results), 3 * len(BUCKETS))
        self.assertTrue(all(r["reason"] == "gps_observation_gap" for r in results))
        self.assertFalse(any(r["status"] == "resolved" for r in results))

    def test_stale_unchanged_source_remains_stale_after_restart(self):
        self.config.update(max_gps_age_seconds=10, max_observation_gap_seconds=60)
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        self.tick(13)
        self.assertTrue(all(r["reason"] == "stale_gps" for r in self.rows("eval_results.csv")))
        self.open_engine()
        self.gps(14, 0, token_seconds=2, Seconds=5)
        self.assertNotIn("5", self.engine.vehicles)
        self.assertEqual(len(self.rows("arrivals.csv")), 1)
        self.gps(15, token_seconds=15)
        self.gps(18, 1)
        self.assertEqual(len(self.rows("arrivals.csv")), 2)

    def test_out_of_order_or_inconsistent_source_not_an_arrival(self):
        for token in (0, 2):
            with self.subTest(token=token):
                if self.engine:
                    self.engine.close()
                    self.engine = None
                import shutil
                shutil.rmtree(self.root / "run", ignore_errors=True)
                self.open_engine()
                self.gps(0)
                self.gps(2, 0)
                self.gps(4, 1, token_seconds=token)
                self.assertEqual(len(self.rows("arrivals.csv")), 1)
                self.assertTrue(all(r["reason"] == "out_of_order_or_inconsistent_gps"
                                    for r in self.rows("eval_results.csv")))

    def test_vendor_estimates_do_not_receive_gps_offset_correction(self):
        self.open_engine()
        self.gps(0)
        arrival = self.at + timedelta(seconds=70)
        self.engine.process({"kind": "estimates", "received_at": stamp(self.at + timedelta(seconds=1)),
                             "error": "", "data": [{"RouteId": 20, "RouteStopId": 11, "Times": [{
                                 "VehicleId": 5, "EstimateTime": f"/Date({int(arrival.timestamp() * 1000)})/",
                                 "Seconds": 69, "IsArriving": False}]}]})
        self.gps(2, 0)
        future = [row for row in self.rows("predictions.csv") if row["horizon_stops"] == "1"]
        expected = int(arrival.timestamp() * 1000) / 1000
        self.assertTrue(all(abs(source_timestamp(row["transloc_predicted_arrival"]) - expected) < .001 for row in future))
        self.gps(4)
        self.gps(6, 1)
        self.assertTrue(all(abs(float(row["transloc_error_seconds"]) - 64) < .001
                            for row in self.rows("eval_results.csv")))

    def test_empty_legacy_run_requires_separate_replay(self):
        self.open_engine()
        self.gps(0)
        frozen = json.loads(self.engine.db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()[0])
        frozen["format_version"] = 2
        with self.engine.db:
            self.engine.db.execute("UPDATE metadata SET value=? WHERE key='frozen'", (json.dumps(frozen),))
        with self.assertRaisesRegex(ValueError, "detector policy changed"):
            self.open_engine()

    def test_scored_legacy_runs_require_separate_replay(self):
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        frozen = json.loads(self.engine.db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()[0])
        frozen["format_version"] = 2
        with self.engine.db:
            self.engine.db.execute("UPDATE metadata SET value=? WHERE key='frozen'", (json.dumps(frozen),))
        with self.assertRaisesRegex(ValueError, "detector policy changed"):
            self.open_engine()

    def test_missing_intermediate_does_not_discard_observed_destination(self):
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        origin = self.rows("arrivals.csv")[0]["event_id"]
        self.gps(4)
        self.gps(10, 2)
        outcomes = [r for r in self.rows("eval_results.csv") if r["origin_event_id"] == origin]
        skipped = [r for r in outcomes if r["horizon_stops"] == "1"]
        observed = [r for r in outcomes if r["horizon_stops"] == "2"]
        self.assertEqual(len(observed), len(BUCKETS))
        self.assertTrue(all(r["status"] == "resolved" and r["missed_intermediate_count"] == "1" for r in observed))
        self.assertTrue(all(r["reason"] == "destination_not_observed" for r in skipped))
        pending = self.engine.db.execute("SELECT COUNT(*) FROM predictions WHERE status='pending'").fetchone()[0]
        self.assertGreater(pending, 0)

    def test_full_circuit_and_missed_destination_never_match_a_later_circuit(self):
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        origin = self.rows("arrivals.csv")[0]["event_id"]
        self.gps(4)
        self.gps(10, 2)
        self.gps(14, 2, Longitude=-83.999)
        self.gps(24, Latitude=32.999, Longitude=-83.999)
        self.gps(28, Latitude=32.999, Longitude=-84)
        self.gps(32, 0)
        self.gps(36, 1)
        outcomes = [r for r in self.rows("eval_results.csv") if r["origin_event_id"] == origin]
        full = [r for r in outcomes if r["horizon_stops"] == "3"]
        self.assertEqual(len(full), len(BUCKETS))
        self.assertTrue(all(r["status"] == "resolved" for r in full))
        self.assertTrue(all(r["reason"] == "destination_not_observed" for r in outcomes if r["horizon_stops"] == "1"))

    def test_stationary_heading_does_not_establish_direction(self):
        self.open_engine()
        for second in (0, 2, 4, 6):
            self.gps(second, 0, Heading=second * 50)
        self.assertEqual(self.rows("arrivals.csv"), [])
        self.assertGreater(self.engine.diagnostics["ambiguous_matches"], 0)

    def test_stop_boundary_jitter_does_not_create_second_arrival(self):
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        for second, lat in [(4,33.00008),(6,33.00011),(8,33.00007)]:
            self.gps(second, Latitude=lat)
        self.assertEqual(len(self.rows("arrivals.csv")), 1)

    def test_restart_and_partial_csv_recovery_preserves_target_circuit(self):
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        origin = self.rows("arrivals.csv")[0]["event_id"]
        self.open_engine()
        self.gps(4)
        self.gps(10, 2)
        before = self.rows("eval_results.csv")
        with (self.root / "run/eval_results.csv").open("a") as output:
            output.write('partial,"unfinished')
        self.open_engine()
        self.assertEqual(self.rows("eval_results.csv"), before)
        self.assertTrue(all(r["origin_event_id"] == origin for r in before))

    def test_route_color_backfills_old_payloads_and_pending_results(self):
        self.model.routes["20"]["name"] = "Red"
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        with self.engine.db:
            for identifier, payload in self.engine.db.execute("SELECT id,payload FROM predictions").fetchall():
                row = json.loads(payload)
                row.pop("route_color", None)
                self.engine.db.execute("UPDATE predictions SET payload=? WHERE id=?", (json.dumps(row), identifier))
        self.open_engine()
        self.assertTrue(all(row["route_color"] == "Red" for row in self.rows("predictions.csv")))
        self.gps(4)
        self.gps(6, 1)
        self.assertTrue(all(row["route_color"] == "Red" for row in self.rows("eval_results.csv")))
        self.assertTrue(all(row["route_color"] == "Red" for row in self.rows("eval_summary.csv")))
        self.engine.write_selection()
        self.assertTrue(all(row["route_color"] == "Red" for row in self.rows("bucket_comparison.csv")))

    def test_recorded_gold_outbound_does_not_visit_return_stops(self):
        routes_path = Path(__file__).resolve().parents[1] / "reference/routes.json"
        self.routes_path = routes_path
        self.model.routes = {"29": load_routes(routes_path)["29"]}
        self.open_engine()
        fixture = Path(__file__).with_name("fixtures") / "gold_outbound_gps.json"
        for observation in json.loads(fixture.read_text()):
            self.engine.process(observation)
        stops = [row["route_stop_id"] for row in self.rows("arrivals.csv")]
        self.assertIn("408", stops)
        self.assertIn("409", stops)
        self.assertFalse(set(stops) & {"417", "416", "415", "413"}, stops)
        self.assertGreater(self.engine.diagnostics["rejected_return_direction_candidates"], 0)

    def test_overlapping_geometry_has_ordered_distinct_anchors(self):
        routes_path = Path(__file__).resolve().parents[1] / "reference/routes.json"
        raw = next(r for r in json.loads(routes_path.read_text()) if r["RouteID"] == 29)
        geometry = RouteGeometry(load_routes(routes_path)["29"]["stops"], decode_polyline(raw["EncodedPolyline"]))
        self.assertTrue(all(b>a for a,b in zip(geometry.anchors,geometry.anchors[1:])))
        self.assertGreater(geometry.anchors[16] - geometry.anchors[4], 3000)

    def test_hung_workers_have_total_deadlines_and_gap_ticks_continue(self):
        import time
        from unittest.mock import patch
        self.at = datetime.now(timezone.utc) - timedelta(seconds=35)
        self.model.training_end = self.at - timedelta(days=1)
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        def worker(kind, url, timeout, stops):
            return EndpointWorker(kind, url, timeout, stops, target=hung_request_worker)
        args = SimpleNamespace(gps_seconds=3, estimate_seconds=10, base_url="https://example.invalid",
                               timeout_seconds=.7, duration_seconds=.1)
        started = time.monotonic()
        with patch("prediction.eval_loop.EndpointWorker", side_effect=worker):
            collect(self.engine, args)
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(all(r["reason"] == "gps_observation_gap" for r in self.rows("eval_results.csv")))
        self.assertEqual(self.engine.diagnostics["request_errors:gps"], 1)
        self.assertEqual(self.engine.diagnostics["request_errors:estimates"], 1)

    def test_geometry_failure_is_reported_without_unsafe_fallback(self):
        self.routes_path.write_text(json.dumps([{"RouteID":20,"EncodedPolyline":"bad"}]))
        self.open_engine()
        self.gps(0)
        self.gps(2,0)
        self.assertEqual(self.rows("arrivals.csv"), [])
        self.assertEqual(self.engine.diagnostics["route_geometry_unusable:20"], 1)

    def test_loop_seam_initialization_does_not_create_permanent_ambiguity(self):
        self.open_engine()
        self.gps(0, Latitude=32.99999)
        self.gps(2, Latitude=33.00012)
        self.assertTrue(self.engine.vehicles["5"]["locked"])
        paths=self.engine.vehicles["5"]["paths"]
        self.assertLess(max(p["p"] for p in paths)-min(p["p"] for p in paths),25)

    def test_snapshot_and_json_replay_reproduce_scores_and_diagnostics(self):
        from prediction.eval_loop import replay
        self.open_engine()
        self.gps(0)
        self.gps(2,0)
        self.gps(4)
        self.gps(10,2)
        expected=self.rows("eval_results.csv")
        for source in ["checkpoint.sqlite3","raw_observations.jsonl"]:
            destination=self.root / ("replay_"+source)
            evaluator=Evaluator(destination,self.model,self.routes_path,self.config)
            try:
                replay(evaluator,self.root / "run" / source)
                with (destination / "eval_results.csv").open() as output:
                    self.assertEqual(list(csv.DictReader(output)),expected)
                self.assertEqual(evaluator.diagnostics,self.engine.diagnostics)
            finally:evaluator.close()

    def test_live_worker_receipts_are_serialized_and_ctrl_c_state_is_durable(self):
        import http.server
        import threading
        import os
        import signal
        class Handler(http.server.BaseHTTPRequestHandler):
            index=0
            def do_GET(self):
                data=[]
                if self.path.startswith("/GetMapVehiclePoints"):
                    lat=32.9997+min(Handler.index,4)*.0001
                    Handler.index+=1
                    data=[{"VehicleID":5,"Name":"2204","RouteID":20,"IsOnRoute":True,
                           "Latitude":lat,"Longitude":-84,"TimeStamp":stamp(datetime.now(timezone.utc))}]
                body=json.dumps(data).encode()
                self.send_response(200);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.open_engine()
        args=SimpleNamespace(gps_seconds=.05,estimate_seconds=.05,timeout_seconds=1,duration_seconds=None,
                             base_url=f"http://127.0.0.1:{server.server_port}")
        timer=threading.Timer(1,lambda:os.kill(os.getpid(),signal.SIGINT));timer.start()
        try:collect(self.engine,args)
        finally:timer.cancel();server.shutdown();server.server_close()
        self.assertGreater(len(self.rows("predictions.csv")),0)
        before=self.rows("predictions.csv")
        self.open_engine()
        self.assertEqual(self.rows("predictions.csv"),before)
        observations=[json.loads(line) for line in (self.root / "run/raw_observations.jsonl").read_text().splitlines()]
        times=[row["received_at"] for row in observations]
        self.assertEqual(times,sorted(times))
        self.assertTrue(all(row["response_completed_at"]<=row["received_at"] for row in observations if row["kind"]!="tick"))
        self.assertFalse((self.root / "run/collector.pid").exists())


if __name__ == "__main__":
    unittest.main()
