from __future__ import annotations

import unittest

from database_game.gateway import _normalize_model_ranges


class GatewayNormalizationTest(unittest.TestCase):
    def test_model_range_is_clamped_without_changing_other_fields(self) -> None:
        payload = {
            "term_key": "regulator_remediation_cost",
            "unit": "CNY",
            "minimum": 10000000,
            "preferred": 0,
            "maximum": 5000000,
            "note": "preserved",
        }
        normalized = _normalize_model_ranges(payload)
        self.assertEqual(normalized["minimum"], "5000000")
        self.assertEqual(normalized["preferred"], "5000000")
        self.assertEqual(normalized["maximum"], "10000000")
        self.assertEqual(normalized["note"], "preserved")

    def test_model_range_is_clamped_to_schema_bounds(self) -> None:
        cash = _normalize_model_ranges(
            {
                "term_key": "debtor_settlement_cash",
                "unit": "CNY",
                "minimum": -100,
                "preferred": 50,
                "maximum": 200,
            }
        )
        ratio = _normalize_model_ranges(
            {
                "term_key": "creditor_recovery_ratio",
                "unit": "ratio",
                "minimum": -0.1,
                "preferred": 0.8,
                "maximum": 1.2,
            }
        )
        self.assertEqual(cash["minimum"], "0")
        self.assertEqual(cash["preferred"], "50")
        self.assertEqual(cash["maximum"], "200")
        self.assertEqual(ratio["minimum"], "0")
        self.assertEqual(ratio["preferred"], "0.8")
        self.assertEqual(ratio["maximum"], "1")

    def test_invalid_empty_evidence_references_are_removed(self) -> None:
        payload = {
            "kind": "evidence_inference",
            "evidence_refs": [
                {"source_table": "dynamic_evidence", "record_id": "1183"},
                {"source_table": "dynamic_evidence", "record_id": None},
                {"source_table": "", "record_id": "2"},
            ]
        }
        normalized = _normalize_model_ranges(payload)
        self.assertEqual(
            normalized["evidence_refs"],
            [{"source_table": "dynamic_evidence", "record_id": "1183"}],
        )
        self.assertEqual(normalized["kind"], "evidence_inference")

        unsupported_inference = _normalize_model_ranges(
            {
                "kind": "evidence_inference",
                "evidence_refs": [{"source_table": "dynamic_evidence", "record_id": None}],
            }
        )
        self.assertEqual(unsupported_inference["evidence_refs"], [])
        self.assertEqual(unsupported_inference["kind"], "industry_prior")


if __name__ == "__main__":
    unittest.main()
