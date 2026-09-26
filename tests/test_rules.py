import unittest

from src.rules import trace_downstream, batch_hold_reasons
from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")
        self.lab = Actor("L-1", "lab")
        self.lab2 = Actor("L-2", "lab")
        self.officer = Actor("Q-1", "quarantine")

    def _batch(self, status="pending_disposition", samples=None):
        return {
            "kind": "consignment",
            "status": status,
            "data": {"samples": samples or []},
        }

    def test_trace_downstream(self):
        links = [
            {"id": "1", "parent_id": None},
            {"id": "2", "parent_id": "1"},
            {"id": "3", "parent_id": "2"},
        ]
        self.assertEqual(trace_downstream(links, "1"), ["1", "2", "3"])

    def test_hold_reasons_cover_pending_unrechecked_positive(self):
        data = {"samples": [{"sample_no": "S-1", "conclusion": "pending"}]}
        reasons = batch_hold_reasons(data)
        self.assertTrue(any("待检" in item for item in reasons))

        data = {"samples": [
            {"sample_no": "S-1", "conclusion": "negative", "rechecks": []}
        ]}
        self.assertTrue(any("复核" in item for item in batch_hold_reasons(data)))

        data = {"samples": [
            {"sample_no": "S-1", "conclusion": "positive", "rechecks": []}
        ]}
        self.assertTrue(any("阳性" in item for item in batch_hold_reasons(data)))

        data = {"samples": [
            {"sample_no": "S-1", "conclusion": "negative",
             "rechecks": [{"tester": "L-2", "conclusion": "negative"}]}
        ]}
        self.assertEqual(batch_hold_reasons(data), [])

    def test_release_blocked_until_all_negatives_rechecked(self):
        entity = self._batch("pending_disposition", [
            {"sample_no": "S-1", "conclusion": "negative", "rechecks": []}
        ])
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.officer, entity, "release", {})

    def test_recheck_must_be_another_tester(self):
        entity = self._batch("pending_disposition", [
            {"sample_no": "S-1", "tester": "L-1", "conclusion": "negative",
             "rechecks": []}
        ])
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(
                self.lab,
                entity,
                "recheck",
                {"sample_no": "S-1", "tester": "L-1", "conclusion": "negative"},
            )

    def test_recheck_after_release_positive_reopens_disposition(self):
        entity = self._batch("released", [
            {"sample_no": "S-1", "tester": "L-1", "conclusion": "negative",
             "rechecks": [{"tester": "L-2", "conclusion": "negative"}]}
        ])
        next_status, patch = self.rules.validate_transition(
            self.lab2,
            entity,
            "recheck",
            {"sample_no": "S-1", "tester": "L-2", "conclusion": "positive"},
        )
        self.assertEqual(next_status, "pending_disposition")
        self.assertTrue(batch_hold_reasons(patch))

    def test_old_single_inspection_action_gone(self):
        with self.assertRaises(InvalidTransition):
            self.rules.validate_transition(
                self.admin,
                {"kind": "consignment", "status": "declared", "data": {}},
                "inspect",
                {"inspector": "I-1", "inspection_result": "clean"},
            )

    def test_quarantine_requires_positive_sample(self):
        entity = self._batch("pending_disposition", [
            {"sample_no": "S-1", "tester": "L-1", "conclusion": "negative",
             "rechecks": [{"tester": "L-2", "conclusion": "negative"}]}
        ])
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.officer, entity, "quarantine", {})

    def test_trace_resolves_batch_code(self):
        rows = [{"id": "uuid-1", "data": {"code": "C-9"}}]

        def lookup(kind, field, value):
            if field == "id":
                return None
            return rows if value == "C-9" else None

        entity = {"kind": "facility", "status": "registered", "data": {}}
        _, patch = self.rules.validate_transition(
            self.officer,
            entity,
            "trace",
            {"consignment_ids": ["C-9"]},
            lookup,
        )
        self.assertEqual(patch["consignment_ids"], ["uuid-1"])

    def test_duplicate_sample_no_rejected(self):
        entity = self._batch("declared", [
            {"sample_no": "S-1", "tester": "L-1", "conclusion": "pending"}
        ])
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(
                self.admin,
                entity,
                "register_sample",
                {"sample_no": "S-1", "sampling_point": "x", "tester": "L-2"},
            )

    def test_viewer_cannot_register_sample(self):
        with self.assertRaises(PermissionDenied):
            self.rules.validate_transition(
                Actor("v", "viewer"),
                self._batch("declared"),
                "register_sample",
                {"sample_no": "S-1", "sampling_point": "x", "tester": "L-1"},
            )


if __name__ == "__main__":
    unittest.main()
