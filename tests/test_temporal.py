import unittest
from betangel_research.temporal import join_asof, timestamp_ms


class TemporalTests(unittest.TestCase):
    def setUp(self):
        self.link = {"source": "ratings", "source_entity_id": "A", "entity_id": "horse-1",
                     "known_at": "2020-01-01T00:00:00Z"}
        self.obs = {"source": "ratings", "source_entity_id": "A", "feature": "rating", "value": 70,
                    "event_time": "2020-01-02T00:00:00Z", "available_at": "2020-01-03T00:00:00Z"}

    def test_late_publication_and_later_revision_invisible(self):
        revision = {**self.obs, "available_at": "2020-01-05T00:00:00Z", "value": 90}
        self.assertEqual(join_asof("horse-1", "2020-01-02T12:00:00Z", [self.obs], [self.link])["features"], {})
        result = join_asof("horse-1", "2020-01-04T00:00:00Z", [self.obs, revision], [self.link])
        self.assertEqual(result["features"]["ratings:rating"]["value"], 70)

    def test_future_identity_and_delay_are_not_available(self):
        future = {**self.link, "known_at": "2020-01-06T00:00:00Z"}
        self.assertEqual(join_asof("horse-1", "2020-01-04T00:00:00Z", [self.obs], [future])["features"], {})
        self.assertEqual(join_asof("horse-1", "2020-01-03T00:00:01Z", [self.obs], [self.link], 2)["features"], {})

    def test_ambiguity_and_naive_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            join_asof("horse-1", "2020-01-04T00:00:00Z", [self.obs],
                      [self.link, {**self.link, "entity_id": "horse-2"}])
        with self.assertRaises(ValueError):
            timestamp_ms("2020-01-01T00:00:00")

    def test_latest_measurement_not_older_late_revision(self):
        newer = {**self.obs, "event_time": "2020-01-04T00:00:00Z",
                 "available_at": "2020-01-04T01:00:00Z", "value": 80}
        old_revision = {**self.obs, "available_at": "2020-01-05T00:00:00Z", "value": 90}
        result = join_asof("horse-1", "2020-01-06T00:00:00Z", [self.obs, newer, old_revision], [self.link])
        self.assertEqual(result["features"]["ratings:rating"]["value"], 80)

    def test_declared_maximum_age(self):
        result = join_asof("horse-1", "2020-01-04T00:00:00Z", [self.obs], [self.link], max_age_seconds=60)
        self.assertEqual(result["features"], {})

    def test_submillisecond_timestamps_rejected_not_rounded(self):
        with self.assertRaises(ValueError):
            timestamp_ms("2020-01-01T00:00:00.000400Z")
        self.assertEqual(timestamp_ms("2020-01-01T00:00:00.001Z") - timestamp_ms("2020-01-01T00:00:00Z"), 1)

    def test_identity_conflicts_independent_of_input_order(self):
        conflict = {**self.link, "entity_id": "horse-2"}
        correction = {**self.link, "known_at": "2020-01-02T00:00:00Z"}
        for versions in [[self.link, conflict, correction], [correction, self.link, conflict]]:
            with self.assertRaises(ValueError):
                join_asof("horse-1", "2020-01-04T00:00:00Z", [self.obs], versions)
