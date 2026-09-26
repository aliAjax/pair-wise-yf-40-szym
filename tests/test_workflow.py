import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("I-1", "inspector")
        self.lab_a = Actor("L-1", "lab")
        self.lab_b = Actor("L-2", "lab")
        self.officer = Actor("Q-1", "quarantine")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_batch(self, code="C-1"):
        return self.service.create(
            self.admin,
            "consignment",
            {"code": code, "origin": "Port-A", "destination": "Farm-B"},
        )

    def _create_facility(self, name="Farm-B"):
        return self.service.create(
            self.admin,
            "facility",
            {"name": name, "address": "County 1"},
        )

    def test_full_workflow_sample_register_recheck_release(self):
        batch = self._create_batch()
        self.assertEqual(batch["status"], "declared")

        # 登记第一条样本（初检待检），批次进入待处置
        batch = self.service.transition(
            self.inspector,
            batch["id"],
            "register_sample",
            {"sample_no": "S-1", "sampling_point": "1号货舱", "tester": "L-1"},
        )
        self.assertEqual(batch["status"], "pending_disposition")
        sample = batch["data"]["samples"][0]
        self.assertEqual(sample["sample_no"], "S-1")
        self.assertEqual(sample["sampling_point"], "1号货舱")
        self.assertEqual(sample["tester"], "L-1")
        self.assertEqual(sample["conclusion"], "pending")

        # 存在待检样本：列出原因，不能放行
        self.assertTrue(batch["data"]["hold_reasons"])

        # 实验室补出阴性初检结论
        batch = self.service.transition(
            self.lab_a,
            batch["id"],
            "record_result",
            {"sample_no": "S-1", "conclusion": "negative"},
        )
        self.assertEqual(batch["status"], "pending_disposition")
        self.assertTrue(
            any("复核" in reason for reason in batch["data"]["hold_reasons"])
        )

        # 由另一名检测人复核为阴性
        batch = self.service.transition(
            self.lab_b,
            batch["id"],
            "recheck",
            {"sample_no": "S-1", "tester": "L-2", "conclusion": "negative"},
        )
        self.assertEqual(batch["data"]["samples"][0]["rechecks"][0]["tester"], "L-2")

        # 全部阴性复核后允许放行
        batch = self.service.transition(self.officer, batch["id"], "release", {})
        self.assertEqual(batch["status"], "released")
        self.assertEqual(batch["data"]["released_by"], "Q-1")

        # 设施按批次编号追溯
        facility = self._create_facility()
        self.service.transition(
            self.officer,
            facility["id"],
            "trace",
            {"consignment_ids": ["C-1"]},
        )
        trace = self.service.trace_consignment(code="C-1")
        self.assertEqual(trace["consignment"]["id"], batch["id"])
        self.assertEqual(trace["traced_facilities"][0]["id"], facility["id"])

        # 已放行后复检改出阳性：批次重新进入待处置
        batch = self.service.transition(
            self.lab_b,
            batch["id"],
            "recheck",
            {"sample_no": "S-1", "tester": "L-2", "conclusion": "positive"},
        )
        self.assertEqual(batch["status"], "pending_disposition")
        self.assertTrue(
            any("阳性" in reason for reason in batch["data"]["hold_reasons"])
        )

        # 原放行记录保留，仍可按批次编号追溯
        self.assertEqual(len(batch["data"]["release_history"]), 1)
        self.assertEqual(batch["data"]["release_history"][0]["released_by"], "Q-1")
        trace = self.service.trace_consignment(code="C-1")
        self.assertEqual(trace["consignment"]["status"], "pending_disposition")
        self.assertEqual(trace["traced_facilities"][0]["data"]["consignment_ids"],
                         [batch["id"]])

        # 阳性批次隔离、销毁
        batch = self.service.transition(self.officer, batch["id"], "quarantine", {})
        self.assertEqual(batch["status"], "quarantined")
        self.assertEqual(batch["data"]["positive_samples"], ["S-1"])
        batch = self.service.transition(
            self.officer,
            batch["id"],
            "destroy",
            {"method": "incineration", "witnessed_by": "W-1"},
        )
        self.assertEqual(batch["status"], "destroyed")

    def test_lab_late_positive_blocks_release(self):
        # 实验室后来补出的阳性样本必须拦住放行
        batch = self._create_batch(code="C-2")
        batch = self.service.transition(
            self.inspector,
            batch["id"],
            "register_sample",
            {
                "sample_no": "S-1",
                "sampling_point": "2号货架",
                "tester": "L-1",
                "conclusion": "negative",
            },
        )
        batch = self.service.transition(
            self.lab_b,
            batch["id"],
            "recheck",
            {"sample_no": "S-1", "tester": "L-2", "conclusion": "negative"},
        )
        # 第二条样本初检待检：批次停在待处置
        batch = self.service.transition(
            self.inspector,
            batch["id"],
            "register_sample",
            {"sample_no": "S-2", "sampling_point": "3号货架", "tester": "L-1"},
        )
        self.assertEqual(batch["status"], "pending_disposition")
        # 实验室随后补出阳性
        batch = self.service.transition(
            self.lab_a,
            batch["id"],
            "record_result",
            {"sample_no": "S-2", "conclusion": "positive"},
        )
        self.assertTrue(
            any("S-2" in reason for reason in batch["data"]["hold_reasons"])
        )

    def test_recheck_negative_after_release_stays_released(self):
        batch = self._create_batch(code="C-3")
        batch = self.service.transition(
            self.inspector,
            batch["id"],
            "register_sample",
            {
                "sample_no": "S-1",
                "sampling_point": "1号货舱",
                "tester": "L-1",
                "conclusion": "negative",
            },
        )
        batch = self.service.transition(
            self.lab_b,
            batch["id"],
            "recheck",
            {"sample_no": "S-1", "tester": "L-2", "conclusion": "negative"},
        )
        batch = self.service.transition(self.officer, batch["id"], "release", {})
        # 放行后再做一次阴性复检：维持放行
        batch = self.service.transition(
            self.lab_b,
            batch["id"],
            "recheck",
            {"sample_no": "S-1", "tester": "L-2", "conclusion": "negative"},
        )
        self.assertEqual(batch["status"], "released")
        self.assertEqual(len(batch["data"]["release_history"]), 1)


if __name__ == "__main__":
    unittest.main()
