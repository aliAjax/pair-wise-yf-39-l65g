import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, is_cluster
from src.service import DomainService


ADMIN = Actor("admin", "admin")
EPI = Actor("epi", "epidemiologist")


def obs(n, observed_at, lat, lon, region="North", event=None, **extra):
    data = {
        "event_id": event or ("E-%d" % n),
        "species": "deer",
        "location": region,
        "observed_at": observed_at,
        "lat": lat,
        "lon": lon,
    }
    data.update(extra)
    return data


class ClusterRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def _submit_three(self, cluster_region="North", observations=None):
        observations = observations or [
            obs(1, "2026-04-01", 40.00, 116.00),
            obs(2, "2026-04-05", 40.01, 116.01),
            obs(3, "2026-04-10", 40.02, 116.02),
        ]
        ids = []
        for data in observations:
            entity = self.service.create(ADMIN, "observation", data)
            self.service.transition(ADMIN, entity["id"], "submit",
                                    {"location": data["location"], "observed_at": data["observed_at"]})
            ids.append(entity["id"])
        cluster = self.service.create(EPI, "cluster", {"region": cluster_region})
        return ids, cluster["id"]

    def test_happy_path_confirms_and_writes_back(self):
        ids, cluster_id = self._submit_three()
        result = self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids})
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["data"]["observation_ids"], ids)
        for observation_id in ids:
            observation = self.service.get(observation_id)
            self.assertEqual(observation["data"]["cluster_id"], cluster_id)
            self.assertEqual(observation["status"], "submitted")

    def test_pairwise_check_catches_far_points_beyond_first_three(self):
        # 前 3 个点互相很近，第 4 个点离其中一个点超过 10km（旧逻辑只看前 3 个会放行）
        points = [
            {"id": "1", "observed_at": "2026-04-01", "lat": 40.0, "lon": 116.0},
            {"id": "2", "observed_at": "2026-04-02", "lat": 40.01, "lon": 116.01},
            {"id": "3", "observed_at": "2026-04-03", "lat": 40.02, "lon": 116.02},
            {"id": "4", "observed_at": "2026-04-04", "lat": 40.30, "lon": 116.02},
        ]
        self.assertTrue(is_cluster(points[:3]))
        self.assertFalse(is_cluster(points))

    def test_all_pairs_compared_not_just_anchor(self):
        # 点 0 与其他点都在 10km 内，但点 1、点 2 相距超过 10km
        points = [
            {"id": "1", "observed_at": "2026-04-01", "lat": 40.00, "lon": 116.00},
            {"id": "2", "observed_at": "2026-04-02", "lat": 40.06, "lon": 115.99},
            {"id": "3", "observed_at": "2026-04-03", "lat": 40.06, "lon": 116.11},
        ]
        self.assertFalse(is_cluster(points))

    def test_cross_region_is_rejected_with_id(self):
        ids, cluster_id = self._submit_three(observations=[
            obs(1, "2026-04-01", 40.00, 116.00, region="North"),
            obs(2, "2026-04-05", 40.01, 116.01, region="South"),
            obs(3, "2026-04-10", 40.02, 116.02, region="North"),
        ])
        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids})
        issue_ids = {item["id"] for item in ctx.exception.issues if item["id"]}
        self.assertIn(ids[1], issue_ids)
        # 原始数据不动
        self.assertEqual(self.service.get(cluster_id)["status"], "draft")

    def test_time_window_over_14_days_is_rejected(self):
        ids, cluster_id = self._submit_three(observations=[
            obs(1, "2026-04-01", 40.00, 116.00),
            obs(2, "2026-04-05", 40.01, 116.01),
            obs(3, "2026-04-20", 40.02, 116.02),
        ])
        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids})
        self.assertTrue(any("14-day" in item["reason"] for item in ctx.exception.issues))
        self.assertIn(ids[2], {item["id"] for item in ctx.exception.issues})

    def test_distance_over_10km_is_rejected(self):
        ids, cluster_id = self._submit_three(observations=[
            obs(1, "2026-04-01", 40.00, 116.00),
            obs(2, "2026-04-02", 40.01, 116.01),
            obs(3, "2026-04-03", 40.50, 116.02),
        ])
        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids})
        self.assertTrue(any("exceeds 10km" in item["reason"] for item in ctx.exception.issues))

    def test_fewer_than_three_submitted_records_fails(self):
        ids, cluster_id = self._submit_three()
        with self.assertRaises(ValidationError):
            self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids[:2]})

    def test_unsubmitted_records_are_flagged(self):
        ids, cluster_id = self._submit_three()
        # 新建一条未提交的第 4 份记录，顶替其中一份
        extra = self.service.create(ADMIN, "observation",
                                    obs(4, "2026-04-06", 40.01, 116.02))
        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_cluster(EPI, cluster_id,
                                         {"observation_ids": ids[:2] + [extra["id"]]})
        self.assertIn(extra["id"], {item["id"] for item in ctx.exception.issues})
        self.assertTrue(any("submitted" in item["reason"] for item in ctx.exception.issues))

    def test_missing_coordinates_are_flagged_with_id(self):
        ids, cluster_id = self._submit_three()
        # 通过仓储直接造一条缺坐标但已提交的记录（创建接口强制要坐标）
        bad = self.repo.create_entity("obs-no-coords", "observation", "captured", {
            "event_id": "E-X", "species": "deer", "location": "North",
            "observed_at": "2026-04-06",
        }, "admin")
        self.repo.update_entity(bad["id"], 1, "submitted", bad["data"])
        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_cluster(EPI, cluster_id,
                                         {"observation_ids": ids[:2] + ["obs-no-coords"]})
        flagged = {item["id"]: item["reason"] for item in ctx.exception.issues}
        self.assertIn("obs-no-coords", flagged)
        self.assertIn("coordinates", flagged["obs-no-coords"])

    def test_record_linked_to_another_confirmed_cluster_is_flagged(self):
        ids, cluster_id = self._submit_three()
        # 先确认第一个事件
        first = self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids})
        self.assertEqual(first["status"], "confirmed")
        # 用同样三份记录再建第二个事件
        second = self.service.create(EPI, "cluster", {"region": "North"})
        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_cluster(EPI, second["id"], {"observation_ids": ids})
        reasons = {item["id"]: item["reason"] for item in ctx.exception.issues}
        for observation_id in ids:
            self.assertIn(observation_id, reasons)
            self.assertIn(cluster_id, reasons[observation_id])
        # 第二个事件仍为 draft，原数据不动
        self.assertEqual(self.service.get(second["id"])["status"], "draft")

    def test_duplicate_confirmation_returns_first_result(self):
        ids, cluster_id = self._submit_three()
        first = self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids})
        second = self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids})
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["version"], second["version"])
        # 即便此时某条记录已挂在别的事件上，重复确认也沿用第一次结果
        third = self.service.create(EPI, "cluster", {"region": "North"})
        self.assertEqual(self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids})["status"],
                         "confirmed")
        self.assertEqual(self.service.get(third["id"])["status"], "draft")

    def test_viewer_cannot_confirm(self):
        ids, cluster_id = self._submit_three()
        with self.assertRaises(PermissionDenied):
            self.service.confirm_cluster(Actor("v", "viewer"), cluster_id,
                                         {"observation_ids": ids})

    def test_preview_does_not_change_data(self):
        ids, cluster_id = self._submit_three()
        preview = self.service.preview_cluster(cluster_id, ids)
        self.assertTrue(preview["valid"])
        self.assertEqual({item["id"] for item in preview["members"]}, set(ids))
        self.assertEqual(self.service.get(cluster_id)["status"], "draft")
        for observation_id in ids:
            self.assertNotIn("cluster_id", self.service.get(observation_id)["data"])

    def test_members_shows_linkage(self):
        ids, cluster_id = self._submit_three()
        self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids})
        view = self.service.cluster_members(cluster_id)
        self.assertEqual({item["id"] for item in view["linked_observations"]}, set(ids))

    def test_confirm_from_dismissed_is_invalid(self):
        ids, cluster_id = self._submit_three()
        self.service.transition(EPI, cluster_id, "dismiss", {"reason": "noise"})
        with self.assertRaises(InvalidTransition):
            self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids})

    def test_boundary_exactly_14_days_passes(self):
        ids, cluster_id = self._submit_three(observations=[
            obs(1, "2026-04-01", 40.00, 116.00),
            obs(2, "2026-04-08", 40.01, 116.01),
            obs(3, "2026-04-15", 40.02, 116.02),
        ])
        result = self.service.confirm_cluster(EPI, cluster_id, {"observation_ids": ids})
        self.assertEqual(result["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
