import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, is_cluster
from src.service import DomainService


class ClusterConfirmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("officer", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _observation(self, event_id, location="North", observed_at="2026-04-01",
                     lat=40.0, lon=116.0, submit=True):
        data = {
            "event_id": event_id,
            "species": "deer",
            "location": location,
            "observed_at": observed_at,
        }
        if lat is not None:
            data["lat"] = lat
        if lon is not None:
            data["lon"] = lon
        entity = self.service.create(self.actor, "observation", data)
        if submit:
            entity = self.service.transition(
                self.actor,
                entity["id"],
                "submit",
                {"location": location, "observed_at": observed_at},
            )
        return entity

    def _cluster(self, region="North"):
        return self.service.create(self.actor, "cluster", {"region": region})

    def _confirm(self, cluster_id, ids):
        return self.service.transition(
            self.actor,
            cluster_id,
            "confirm_cluster",
            {"observation_ids": ids, "centroid": [40.0, 116.0]},
        )

    def _three_observations(self):
        return [
            self._observation("E-1", observed_at="2026-04-01", lat=40.0, lon=116.0),
            self._observation("E-2", observed_at="2026-04-03", lat=40.01, lon=116.01),
            self._observation("E-3", observed_at="2026-04-05", lat=40.02, lon=116.02),
        ]

    def test_confirm_writes_cluster_id_back(self):
        observations = self._three_observations()
        cluster = self._cluster()
        confirmed = self._confirm(cluster["id"], [o["id"] for o in observations])
        self.assertEqual(confirmed["status"], "confirmed")
        for observation in observations:
            reloaded = self.service.get(observation["id"])
            self.assertEqual(reloaded["data"].get("cluster_id"), cluster["id"])
            self.assertEqual(reloaded["status"], "submitted")

    def test_cross_region_rejected_and_data_untouched(self):
        observations = self._three_observations()
        outsider = self._observation(
            "E-4", location="South", observed_at="2026-04-06", lat=40.03, lon=116.03
        )
        cluster = self._cluster()
        ids = [o["id"] for o in observations] + [outsider["id"]]
        with self.assertRaises(ValidationError) as ctx:
            self._confirm(cluster["id"], ids)
        self.assertIn(outsider["id"], str(ctx.exception))
        self.assertEqual(self.service.get(cluster["id"])["status"], "draft")
        for observation in observations + [outsider]:
            self.assertNotIn("cluster_id", self.service.get(observation["id"])["data"])

    def test_time_window_over_14_days_rejected(self):
        observations = [
            self._observation("E-1", observed_at="2026-04-01"),
            self._observation("E-2", observed_at="2026-04-05"),
            self._observation("E-3", observed_at="2026-04-30"),
        ]
        cluster = self._cluster()
        with self.assertRaises(ValidationError) as ctx:
            self._confirm(cluster["id"], [o["id"] for o in observations])
        self.assertIn("14", str(ctx.exception))

    def test_distance_over_10km_rejected(self):
        observations = [
            self._observation("E-1", lat=40.0, lon=116.0),
            self._observation("E-2", lat=40.01, lon=116.01),
            self._observation("E-3", lat=40.5, lon=116.5),
        ]
        cluster = self._cluster()
        with self.assertRaises(ValidationError) as ctx:
            self._confirm(cluster["id"], [o["id"] for o in observations])
        self.assertIn("10", str(ctx.exception))

    def test_too_few_observations_rejected(self):
        observations = [
            self._observation("E-1"),
            self._observation("E-2", observed_at="2026-04-02", lat=40.01, lon=116.01),
        ]
        cluster = self._cluster()
        with self.assertRaises(ValidationError) as ctx:
            self._confirm(cluster["id"], [o["id"] for o in observations])
        self.assertIn("3", str(ctx.exception))

    def test_unsubmitted_observation_rejected(self):
        observations = self._three_observations()
        draft = self._observation("E-4", submit=False)
        cluster = self._cluster()
        ids = [o["id"] for o in observations] + [draft["id"]]
        with self.assertRaises(ValidationError) as ctx:
            self._confirm(cluster["id"], ids)
        self.assertIn(draft["id"], str(ctx.exception))

    def test_missing_coordinates_rejected_with_ids(self):
        observations = self._three_observations()
        no_coords = self._observation("E-4", lat=None, lon=None)
        cluster = self._cluster()
        ids = [o["id"] for o in observations] + [no_coords["id"]]
        with self.assertRaises(ValidationError) as ctx:
            self._confirm(cluster["id"], ids)
        message = str(ctx.exception)
        self.assertIn("coordinates", message)
        self.assertIn(no_coords["id"], message)

    def test_unknown_observation_rejected(self):
        observations = self._three_observations()
        cluster = self._cluster()
        ids = [o["id"] for o in observations] + ["missing-id"]
        with self.assertRaises(ValidationError) as ctx:
            self._confirm(cluster["id"], ids)
        self.assertIn("missing-id", str(ctx.exception))

    def test_already_in_other_confirmed_cluster_rejected(self):
        observations = self._three_observations()
        first = self._cluster()
        self._confirm(first["id"], [o["id"] for o in observations])
        second = self._cluster()
        with self.assertRaises(ValidationError) as ctx:
            self._confirm(second["id"], [o["id"] for o in observations])
        message = str(ctx.exception)
        self.assertIn(first["id"], message)
        for observation in observations:
            self.assertIn(observation["id"], message)
        for observation in observations:
            self.assertEqual(
                self.service.get(observation["id"])["data"].get("cluster_id"),
                first["id"],
            )
        self.assertEqual(self.service.get(second["id"])["status"], "draft")

    def test_duplicate_confirm_returns_first_result(self):
        observations = self._three_observations()
        cluster = self._cluster()
        ids = [o["id"] for o in observations]
        first = self._confirm(cluster["id"], ids)
        second = self._confirm(cluster["id"], ids)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(second["status"], "confirmed")

    def test_confirm_with_different_set_after_confirmed_conflicts(self):
        observations = self._three_observations()
        extra = self._observation("E-4", observed_at="2026-04-06", lat=40.03, lon=116.03)
        cluster = self._cluster()
        ids = [o["id"] for o in observations]
        self._confirm(cluster["id"], ids)
        with self.assertRaises(ConflictError):
            self._confirm(cluster["id"], ids + [extra["id"]])

    def test_cluster_observations_returns_linked_records(self):
        observations = self._three_observations()
        cluster = self._cluster()
        self._confirm(cluster["id"], [o["id"] for o in observations])
        related = self.service.cluster_observations(cluster["id"])
        self.assertEqual(related["cluster"]["id"], cluster["id"])
        self.assertEqual(
            sorted(o["id"] for o in related["observations"]),
            sorted(o["id"] for o in observations),
        )

    def test_epidemiologist_can_confirm(self):
        observations = self._three_observations()
        cluster = self._cluster()
        confirmed = self.service.transition(
            Actor("epi-1", "epidemiologist"),
            cluster["id"],
            "confirm_cluster",
            {"observation_ids": [o["id"] for o in observations], "centroid": [40.0, 116.0]},
        )
        self.assertEqual(confirmed["status"], "confirmed")

    def test_is_cluster_checks_all_points(self):
        points = [
            {"observed_at": "2026-01-01", "lat": 30.0, "lon": 120.0},
            {"observed_at": "2026-01-02", "lat": 30.01, "lon": 120.01},
            {"observed_at": "2026-01-03", "lat": 30.02, "lon": 120.02},
            {"observed_at": "2026-01-04", "lat": 35.0, "lon": 125.0},
        ]
        self.assertFalse(is_cluster(points))
        self.assertTrue(is_cluster(points[:3]))


if __name__ == "__main__":
    unittest.main()
