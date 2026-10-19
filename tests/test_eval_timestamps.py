"""Regression checks for the GT GPS clock, independent of live API access."""

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from prediction.eval_loop import BUCKETS, Evaluator, source_timestamp, stamp


class GPSClockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.routes_path = self.root / "routes.json"
        self.routes_path.write_text("[]")
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
                 "Latitude": 33 + index * .001 if index is not None else 33.0005,
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
        self.gps(16, 1)
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

    def test_empty_legacy_run_upgrades_and_resumes(self):
        self.open_engine()
        self.gps(0)
        frozen = json.loads(self.engine.db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()[0])
        frozen["format_version"] = 2
        with self.engine.db:
            self.engine.db.execute("UPDATE metadata SET value=? WHERE key='frozen'", (json.dumps(frozen),))
        self.open_engine()
        self.assertEqual(self.engine.vehicles, {})
        version = json.loads(self.engine.db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()[0])
        self.assertEqual(version["format_version"], 3)
        self.gps(1)
        self.gps(2, 0)
        self.assertEqual(len(self.rows("arrivals.csv")), 1)

    def test_scored_legacy_runs_require_separate_replay(self):
        self.open_engine()
        self.gps(0)
        self.gps(2, 0)
        frozen = json.loads(self.engine.db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()[0])
        frozen["format_version"] = 2
        with self.engine.db:
            self.engine.db.execute("UPDATE metadata SET value=? WHERE key='frozen'", (json.dumps(frozen),))
        with self.assertRaisesRegex(ValueError, "GPS clock policy changed"):
            self.open_engine()


if __name__ == "__main__":
    unittest.main()
