"""Offline snapshot checks for startup without GPS or persistent state."""

import csv
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from prediction.live_arrivals import (
    collect_snapshot, parse_estimates, parse_vehicle_estimates, predict_arrivals,
    select_route, select_stop, load_offsets, route_offset, OFFSET_COLUMNS,
)


class LiveArrivalsTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 22, 16, tzinfo=timezone.utc)
        self.stops = [{"id": str(100 + i), "name": name} for i, name in enumerate("ABCD")]
        self.calls = []

        def lookup(rid, origin, horizon, width, at):
            self.calls.append((origin, horizon, width, at))
            return (horizon * 100, 50, 4), False, 0

        self.model = SimpleNamespace(routes={"20": {"name": "Red", "stops": self.stops}}, lookup=lookup)

    def entry(self, sid, seconds, **extra):
        return {"RouteStopID": sid, "VehicleId": 5, "Seconds": seconds, **extra}

    def stop_data(self, seconds=420, **extra):
        return [{"RouteId": 20, "RouteStopId": 103,
                 "Times": [self.entry(103, seconds, **extra)]}]

    def upcoming(self, entries, at=None):
        return parse_vehicle_estimates([{"VehicleID": 5, "Estimates": entries}], "20",
                                       self.stops, {"5"}, at or self.at)

    def predict(self, entries, destination=3, seconds=420, now=None,
                estimates_at=None, upcoming_at=None, offset_seconds=0, **extra):
        estimates_at = estimates_at or self.at
        upcoming_at = upcoming_at or self.at
        data = self.stop_data(seconds, **extra)
        data[0]["RouteStopId"] = 100 + destination
        estimates = parse_estimates(data, "20", estimates_at, str(100 + destination))
        return predict_arrivals(self.model, "20", destination, estimates, estimates_at,
                                self.upcoming(entries, upcoming_at), upcoming_at,
                                now or self.at, width=120, offset_seconds=offset_seconds)

    def test_offsets_shift_final_hybrid_once_and_leave_transloc_unchanged(self):
        for offset in (30, -30, 12.5):
            row = self.predict([self.entry(101, 40)], offset_seconds=offset)[0]
            self.assertEqual(row["predicted_seconds"], round(240 + offset))
            self.assertEqual(row["transloc_seconds"], 420)
            self.assertEqual(row["applied_offset_seconds"], offset)
            self.assertEqual(row["anchor_seconds"], 40)

    def test_offset_applies_to_transloc_fallback_and_next_stop(self):
        fallback = self.predict([], offset_seconds=30)[0]
        next_stop = self.predict([self.entry(103, 420)], offset_seconds=-30)[0]
        self.assertEqual(fallback["predicted_seconds"], 450)
        self.assertEqual(next_stop["predicted_seconds"], 390)

    def test_offset_clamps_to_zero_and_does_not_delay_an_arriving_bus(self):
        row = self.predict([self.entry(101, 40)], offset_seconds=-500)[0]
        self.assertEqual(row["predicted_seconds"], 0)
        self.assertEqual(row["applied_offset_seconds"], -240)
        arriving = self.predict([], seconds=0, IsArriving=True, offset_seconds=30)[0]
        self.assertEqual(arriving["predicted_seconds"], 0)
        self.assertEqual(arriving["offset_seconds"], 30)
        self.assertEqual(arriving["applied_offset_seconds"], 0)

    def test_first_snapshot_produces_hybrid_without_gps_or_prior_polls(self):
        row = self.predict([self.entry(101, 40)])[0]
        self.assertEqual(row["predicted_seconds"], 240)
        self.assertEqual(row["transloc_seconds"], 420)
        self.assertEqual(row["source"], "hybrid")
        self.assertEqual(row["model_bucket_minutes"], 120)
        self.assertEqual(self.calls, [(1, 2, 120, self.at + timedelta(seconds=40))])

    def test_arriving_stop_is_skipped_so_remaining_dwell_stays_in_first_leg(self):
        row = self.predict([self.entry(100, 0, IsArriving=True), self.entry(101, 40)])[0]
        self.assertEqual(row["anchor_stop_id"], "101")
        self.assertEqual(row["predicted_seconds"], 240)
        self.assertEqual(len(self.calls), 1)

    def test_next_destination_preserves_selected_stop_estimate(self):
        row = self.predict([self.entry(103, 45)], seconds=40)[0]
        self.assertEqual(row["source"], "transloc-next-stop")
        self.assertEqual(row["predicted_seconds"], 40)
        self.assertEqual(self.calls, [])

    def test_route_wrap_uses_forward_horizon(self):
        row = self.predict([self.entry(100, 40)], destination=1)[0]
        self.assertEqual(row["predicted_seconds"], 140)
        self.assertEqual(self.calls[0][:2], (0, 1))

    def test_at_target_returns_current_arrival_not_future_circuit(self):
        row = self.predict([self.entry(103, 0, IsArriving=True), self.entry(100, 40)],
                           seconds=0, IsArriving=True)[0]
        self.assertEqual(row["source"], "transloc-arriving")
        self.assertEqual(row["predicted_seconds"], 0)
        self.assertEqual(self.calls, [])

    def test_does_not_anchor_after_selected_occurrence(self):
        row = self.predict([self.entry(100, 80)], seconds=40)[0]
        self.assertEqual(row["source"], "transloc-fallback")
        self.assertIn("precedes", row["reason"])
        self.assertEqual(self.calls, [])

    def test_response_order_does_not_determine_anchor(self):
        row = self.predict([self.entry(102, 80), self.entry(101, 40)])[0]
        self.assertEqual(row["anchor_stop_id"], "101")

    def test_tied_earliest_different_stops_are_ambiguous(self):
        row = self.predict([self.entry(101, 40), self.entry(102, 40)])[0]
        self.assertEqual(row["source"], "transloc-fallback")
        self.assertIn("ambiguous", row["reason"])

    def test_only_arriving_entries_fall_back_without_inventing_origin_time(self):
        row = self.predict([self.entry(101, 0, IsArriving=True)])[0]
        self.assertEqual(row["source"], "transloc-fallback")
        self.assertEqual(self.calls, [])

    def test_missing_model_preserves_original(self):
        self.model.lookup = lambda *args: (None, True, 0)
        row = self.predict([self.entry(101, 40)])[0]
        self.assertEqual(row["source"], "transloc-fallback")
        self.assertEqual(row["predicted_seconds"], 420)

    def test_bucket_fallback_is_explicit(self):
        self.model.lookup = lambda *args: ((200, 50, 4), True, 0)
        row = self.predict([self.entry(101, 40)])[0]
        self.assertTrue(row["bucket_fallback"])
        self.assertEqual(row["model_bucket_minutes"], 0)

    def test_stale_upcoming_data_preserves_fresh_selected_estimate(self):
        now = self.at + timedelta(seconds=31)
        row = self.predict([self.entry(101, 40)], now=now, estimates_at=now)[0]
        self.assertEqual(row["source"], "transloc-fallback")

    def test_stale_selected_data_is_not_displayed(self):
        rows = self.predict([self.entry(101, 40)], now=self.at + timedelta(seconds=31))
        self.assertEqual(rows, [])

    def test_passed_future_estimate_is_not_reused_as_anchor(self):
        row = self.predict([self.entry(101, 5), self.entry(102, 40)],
                           now=self.at + timedelta(seconds=10))[0]
        self.assertEqual(row["anchor_stop_id"], "102")
        self.assertEqual(row["predicted_seconds"], 130)

    def test_unknown_route_stops_or_mismatched_vehicle_invalidate_anchor(self):
        for entry in (self.entry(999, 40), self.entry(101, 40, VehicleId=6)):
            row = self.predict([entry, self.entry(102, 80)])[0]
            self.assertEqual(row["source"], "transloc-fallback")

    def test_parser_filters_schedules_departed_other_routes_and_prefers_absolute(self):
        data = self.stop_data()
        data[0]["Times"] = [
            self.entry(103, 999, EstimateTime=f"/Date({int((self.at.timestamp() + 40) * 1000)})/"),
            self.entry(103, 500), {"Seconds": 50},
            self.entry(103, 10, VehicleId=6, IsDeparted=True),
        ]
        data.append({"RouteId": 21, "RouteStopId": 103, "Times": [self.entry(103, 20)]})
        estimates = parse_estimates(data, "20", self.at, "103")
        self.assertEqual(set(estimates), {"5"})
        self.assertEqual([e["arrival"] for e in estimates["5"]],
                         [self.at.timestamp() + 40, self.at.timestamp() + 500])

    def test_each_poll_uses_only_its_snapshot(self):
        first = self.predict([self.entry(101, 40)])[0]
        later = self.predict([self.entry(102, 60)])[0]
        self.assertEqual(first["predicted_seconds"], 240)
        self.assertEqual(later["predicted_seconds"], 160)
        self.assertEqual(later["model_horizon"], 1)

    def test_collect_snapshot_makes_two_requests_and_hybrid_on_first_call(self):
        requests = []
        now = datetime.now(timezone.utc)

        def fake_fetch(base, method, params, timeout):
            requests.append((method, params))
            if method == "GetStopArrivalTimes":
                return self.stop_data(), now
            if method == "GetVehicleRouteStopEstimates":
                return [{"VehicleID": 5, "Estimates": [self.entry(101, 40)]}], now
            raise AssertionError("Prediction should not request GPS")

        with patch("prediction.live_arrivals.fetch", side_effect=fake_fetch):
            with ThreadPoolExecutor(max_workers=2) as pool:
                snapshot = collect_snapshot(pool, self.model, "20", 3, "test", 10, 30, 120)
        self.assertEqual(snapshot["buses"][0]["source"], "hybrid")
        self.assertEqual([r[0] for r in requests], ["GetStopArrivalTimes", "GetVehicleRouteStopEstimates"])
        self.assertEqual(requests[0][1]["routeStopIDs"], "103")
        self.assertEqual(requests[1][1]["vehicleIdStrings"], "5")

    def test_upcoming_request_failure_still_displays_transloc(self):
        now = datetime.now(timezone.utc)

        def fake_fetch(base, method, params, timeout):
            if method == "GetStopArrivalTimes":
                return self.stop_data(), now
            raise OSError("test outage")

        with patch("prediction.live_arrivals.fetch", side_effect=fake_fetch):
            with ThreadPoolExecutor(max_workers=2) as pool:
                snapshot = collect_snapshot(pool, self.model, "20", 3, "test", 10, 30, 120)
        self.assertEqual(snapshot["buses"][0]["source"], "transloc-fallback")
        self.assertIn("test outage", snapshot["warnings"][0])

    def test_matching_rejects_ambiguous_names(self):
        stops = [{"id": "1", "name": "Klaus EB"}, {"id": "2", "name": "Klaus WB"}]
        self.assertEqual(select_stop(stops, "klaus eb"), 0)
        self.assertEqual(select_stop(stops, "2"), 1)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            select_stop(stops, "Klaus")
        self.assertEqual(select_route({"20": {"name": "Red"}}, "red"), "20")


class OffsetTableTests(unittest.TestCase):
    def test_eastern_blocks_boundaries_overnight_and_missing_route(self):
        offsets = {"red": list(range(10))}
        # UTC 08:00 is 4 AM EDT; UTC 00:00 on the following day is 8 PM.
        at = datetime(2026, 10, 22, 8, tzinfo=timezone.utc)
        self.assertEqual(route_offset(offsets, "RED", at), (0, "4-6 AM"))
        self.assertEqual(route_offset(offsets, "Red", at + timedelta(hours=2)), (1, "6-8 AM"))
        self.assertEqual(route_offset(offsets, "Red", at + timedelta(hours=18)), (9, "10 PM-12 AM"))
        self.assertEqual(route_offset(offsets, "Red", at + timedelta(hours=20)), (0, None))
        self.assertEqual(route_offset(offsets, "Blue", at + timedelta(hours=2)), (0, "6-8 AM"))
        # Standard time must use Eastern time too (UTC 9:00 is 4 AM EST).
        winter = datetime(2026, 12, 1, 9, tzinfo=timezone.utc)
        self.assertEqual(route_offset(offsets, "Red", winter), (0, "4-6 AM"))

    def test_table_reload_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "offsets.csv"

            def write(values, extra=None):
                with path.open("w", newline="") as output:
                    writer = csv.writer(output)
                    writer.writerow(["route_color", *OFFSET_COLUMNS])
                    writer.writerow(["Red", *values])
                    if extra:
                        writer.writerow(extra)

            write([0] * 10)
            self.assertEqual(load_offsets(path)["red"], [0] * 10)
            write([30, -30, 1.5, *([0] * 7)])
            self.assertEqual(load_offsets(path)["red"][:3], [30, -30, 1.5])
            for bad in ("", "oops", "nan", "inf"):
                write([bad, *([0] * 9)])
                with self.assertRaisesRegex(ValueError, "Invalid offset"):
                    load_offsets(path)
            write([0] * 10, ["red", *([0] * 10)])
            with self.assertRaisesRegex(ValueError, "duplicate route"):
                load_offsets(path)
            path.write_text("route_color,bad_header\\nRed,1\\n")
            with self.assertRaisesRegex(ValueError, "columns"):
                load_offsets(path)


if __name__ == "__main__":
    unittest.main()
