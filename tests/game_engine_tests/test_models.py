from __future__ import annotations

import unittest
from decimal import Decimal

from pydantic import ValidationError

from database_game.models import CompleteAnalysisReport, DecisionMatrixReport, Dimension, FactItem, FactStatus, TermRange


class GameModelTest(unittest.TestCase):
    def test_confirmed_fact_requires_database_evidence(self) -> None:
        with self.assertRaises(ValidationError):
            FactItem(
                fact_id="fact-1",
                dimension=Dimension.ASSET,
                statement="项目拥有一宗土地。",
                status=FactStatus.CONFIRMED,
                confidence=Decimal("0.9"),
            )

    def test_term_range_must_be_ordered(self) -> None:
        with self.assertRaises(ValidationError):
            TermRange(
                term_key="total_investment",
                unit="CNY",
                minimum=Decimal("100"),
                preferred=Decimal("80"),
                maximum=Decimal("90"),
            )

    def test_final_report_has_exactly_four_modules(self) -> None:
        properties = DecisionMatrixReport.model_json_schema()["properties"]
        self.assertEqual(
            set(properties),
            {"decision_matrix", "liquidation_value", "dynamic_paths", "red_team_audit"},
        )

    def test_complete_report_labels_unlabeled_simulated_assumption(self) -> None:
        values = {
            "title": "报告",
            "executive_summary": "附条件推进。",
            "project_overview": "项目概况。",
            "asset_analysis": "资产分析。",
            "economic_analysis": "经济分析。",
            "legal_analysis": "法律分析。",
            "game_analysis": "债权人处置压力属于模拟假设。",
            "liquidation_analysis": "清算分析。",
            "plan_a_analysis": "方案 A。",
            "plan_b_analysis": "方案 B。",
            "red_team_analysis": "红队分析。",
            "investment_recommendation": "建议暂停。",
            "action_plan": ["核验。"],
            "simulated_assumptions": ["债权人可能存在处置压力。"],
        }
        report = CompleteAnalysisReport.model_validate(values)
        self.assertTrue(report.game_analysis.startswith("【模拟假设】"))
        self.assertTrue(report.simulated_assumptions[0].startswith("【模拟假设】"))

        values["red_team_analysis"] = "若抵押失败，回收率可能降低20%-40%。"
        report = CompleteAnalysisReport.model_validate(values)
        self.assertTrue(report.red_team_analysis.startswith("【模拟假设】"))


if __name__ == "__main__":
    unittest.main()
