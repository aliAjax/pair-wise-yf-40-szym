import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class SampleWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.lab1 = Actor("lab-1", "lab")
        self.lab2 = Actor("lab-2", "lab")

    def tearDown(self):
        self.tmp.cleanup()

    def _consignment(self, code="C-100"):
        return self.service.create(
            self.admin,
            "consignment",
            {"code": code, "origin": "Port-A", "destination": "Farm-B"},
        )

    def _sample(self, consignment_id, sample_no="S-1", tester="lab-1"):
        return self.service.create(
            self.lab1,
            "sample",
            {
                "consignment_id": consignment_id,
                "sample_no": sample_no,
                "sampling_point": "P-1",
                "tester": tester,
            },
        )

    def _clear_sample(self, consignment_id, sample_no="S-1"):
        sample = self._sample(consignment_id, sample_no)
        self.service.transition(self.lab1, sample["id"], "test", {"conclusion": "negative"})
        self.service.transition(
            self.lab2, sample["id"], "retest", {"retest_conclusion": "negative"}
        )
        return sample

    def test_pending_sample_holds_consignment_with_reason(self):
        consignment = self._consignment()
        self._sample(consignment["id"])
        consignment = self.service.get(consignment["id"])
        self.assertEqual(consignment["status"], "pending_disposal")
        self.assertIn("sample S-1 pending", consignment["data"]["hold_reasons"])
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, consignment["id"], "release", {})

    def test_positive_sample_holds_consignment_and_blocks_release(self):
        consignment = self._consignment()
        sample = self._sample(consignment["id"])
        self.service.transition(self.lab1, sample["id"], "test", {"conclusion": "positive"})
        consignment = self.service.get(consignment["id"])
        self.assertEqual(consignment["status"], "pending_disposal")
        self.assertIn("sample S-1 positive", consignment["data"]["hold_reasons"])
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, consignment["id"], "release", {})

    def test_release_requires_reviewed_negative_samples(self):
        consignment = self._consignment()
        sample = self._sample(consignment["id"])
        self.service.transition(self.lab1, sample["id"], "test", {"conclusion": "negative"})
        consignment = self.service.get(consignment["id"])
        # 初检阴性但未复检：放行仍被拦截
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, consignment["id"], "release", {})
        self.service.transition(
            self.lab2, sample["id"], "retest", {"retest_conclusion": "negative"}
        )
        consignment = self.service.get(consignment["id"])
        self.assertEqual(consignment["status"], "declared")
        self.assertEqual(consignment["data"]["hold_reasons"], [])
        released = self.service.transition(self.admin, consignment["id"], "release", {})
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["released_by"], "admin")

    def test_retest_requires_a_different_tester(self):
        consignment = self._consignment()
        sample = self._sample(consignment["id"])
        self.service.transition(self.lab1, sample["id"], "test", {"conclusion": "negative"})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.lab1, sample["id"], "retest", {"retest_conclusion": "negative"}
            )
        reviewed = self.service.transition(
            self.lab2, sample["id"], "retest", {"retest_conclusion": "negative"}
        )
        self.assertEqual(reviewed["status"], "reviewed")
        self.assertEqual(reviewed["data"]["retester"], "lab-2")

    def test_sample_registration_rules(self):
        consignment = self._consignment()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.lab1,
                "sample",
                {
                    "consignment_id": "missing",
                    "sample_no": "S-9",
                    "sampling_point": "P-1",
                    "tester": "lab-1",
                },
            )
        self._sample(consignment["id"])
        with self.assertRaises(ConflictError):
            self._sample(consignment["id"])

    def test_positive_retest_after_release_reholds_and_keeps_trace(self):
        consignment = self._consignment()
        sample = self._clear_sample(consignment["id"])
        released = self.service.transition(self.admin, consignment["id"], "release", {})
        self.assertEqual(released["status"], "released")

        # 放行后复检改出阳性：批次重新进入待处置
        self.service.transition(
            self.lab1, sample["id"], "retest", {"retest_conclusion": "positive"}
        )
        held = self.service.get(consignment["id"])
        self.assertEqual(held["status"], "pending_disposal")
        self.assertIn("sample S-1 positive", held["data"]["hold_reasons"])
        self.assertEqual(held["data"]["released_by"], "admin")

        # 原放行记录保留在审计时间线中
        actions = [
            (entry["action"], entry["to_status"])
            for entry in self.service.audit_log(consignment["id"])
        ]
        self.assertIn(("release", "released"), actions)
        self.assertIn(("hold", "pending_disposal"), actions)

        # 设施追溯仍按批次编号查到这批货物
        facility = self.service.create(
            self.admin, "facility", {"name": "Farm-B", "address": "County 1"}
        )
        traced = self.service.transition(
            self.admin,
            facility["id"],
            "trace",
            {"consignment_ids": ["C-100"]},
        )
        self.assertEqual(traced["status"], "traced")
        found = traced["data"]["traced_consignments"]
        self.assertEqual(found[0]["id"], consignment["id"])
        self.assertEqual(found[0]["status"], "pending_disposal")

    def test_retest_can_overturn_positive_and_release(self):
        consignment = self._consignment()
        sample = self._sample(consignment["id"])
        self.service.transition(self.lab1, sample["id"], "test", {"conclusion": "positive"})
        self.assertEqual(self.service.get(consignment["id"])["status"], "pending_disposal")
        self.service.transition(
            self.lab2, sample["id"], "retest", {"retest_conclusion": "negative"}
        )
        consignment = self.service.get(consignment["id"])
        self.assertEqual(consignment["status"], "declared")
        released = self.service.transition(self.admin, consignment["id"], "release", {})
        self.assertEqual(released["status"], "released")


if __name__ == "__main__":
    unittest.main()
