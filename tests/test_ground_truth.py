"""Ground-truth cohort and scoring checks without API calls."""

import unittest

from prediction.export_ground_truth import journeys, score


class GroundTruthTests(unittest.TestCase):
    def setUp(self):
        self.frozen = {"format_version": 4, "model_hash": "test"}
        self.routes = {"20": {"name": "Gold", "stops": [
            {"id": "10", "name": "A"}, {"id": "11", "name": "B"}]}}
        self.row = dict(origin_event_id="1:5:0", horizon_stops=1, bucket_minutes=0,
                        route_id="20", vehicle_id="5", origin_route_stop_id="10",
                        destination_route_stop_id="11", origin_arrival="2026-10-19T14:00:00+00:00",
                        status="resolved", actual_arrival="2026-10-19T14:01:00+00:00",
                        actual_lower_bound="2026-10-19T14:00:57+00:00", actual_elapsed_seconds=60,
                        mean_elapsed_seconds=80, fallback_used=0, sample_count=20)

    def rows(self, variants=None):
        return list(journeys(self.frozen, variants or [self.row], self.routes))

    def test_variants_collapse_and_labels_survive_unavailable_baseline(self):
        variants = [dict(self.row, status="unavailable", mean_elapsed_seconds=""),
                    dict(self.row, bucket_minutes=60)]
        rows = self.rows(variants)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actual_elapsed_seconds"], 60)
        self.assertEqual(rows[0]["baseline_predicted_elapsed_seconds"], "")
        self.assertEqual(rows[0]["sample_id"], self.rows(list(reversed(variants)))[0]["sample_id"])

    def test_local_features_and_bounds(self):
        row = self.rows()[0]
        self.assertEqual(row["local_hour"], 10)
        self.assertEqual(row["weekday"], 0)
        self.assertEqual(row["route_fraction_ahead"], .5)
        self.assertEqual(row["actual_elapsed_lower_seconds"], 57)
        self.assertEqual(row["arrival_uncertainty_seconds"], 3)

    def test_unresolved_never_scored_and_missing_predictions_reduce_coverage(self):
        rows = self.rows() + self.rows([dict(self.row, origin_event_id="2", status="pending")])
        rows += self.rows([dict(self.row, origin_event_id="3")])
        predictions = {rows[0]["sample_id"]: 50, rows[1]["sample_id"]: 50}
        details, summaries = score(rows, predictions)
        overall = summaries[0]
        self.assertEqual(details[0]["error_seconds"], -10)
        self.assertEqual(details[1]["error_seconds"], "")
        self.assertEqual(overall["scored_count"], 1)
        self.assertEqual(overall["resolved_prediction_coverage"], .5)
        self.assertEqual(overall["mae_seconds"], 10)
        self.assertEqual(overall["paired_improvement_seconds"], 10)

    def test_no_label_or_no_predictions_gives_blank_metrics(self):
        rows = self.rows([dict(self.row, status="excluded", reason="gps_observation_gap")])
        self.assertEqual(rows[0]["actual_elapsed_seconds"], "")
        details, summaries = score(rows, {})
        self.assertEqual(summaries[0]["mae_seconds"], "")
        self.assertEqual(summaries[0]["resolved_prediction_coverage"], "")


if __name__ == "__main__":
    unittest.main()
