from __future__ import annotations

import unittest

from database_game.models import CompleteAnalysisReport
from database_game.reporting import build_assumption_disclosures, render_complete_report


class CompleteReportRenderingTest(unittest.TestCase):
    def test_markdown_contains_all_required_sections_and_disclosures(self) -> None:
        report = CompleteAnalysisReport(
            title="完整分析报告",
            executive_summary="执行摘要。",
            project_overview="项目概况。",
            asset_analysis="资产分析。",
            economic_analysis="经济分析。",
            legal_analysis="法律分析。",
            game_analysis="博弈分析。",
            liquidation_analysis="清算分析。",
            plan_a_analysis="方案 A。",
            plan_b_analysis="方案 B。",
            red_team_analysis="红队分析。",
            investment_recommendation="附条件推进。",
            action_plan=["先核验债权。"],
            data_gaps=["缺少估值。"],
            simulated_assumptions=["【模拟假设】银行存在时间压力。"],
        )
        markdown = render_complete_report(
            report,
            {"project_name": "测试项目", "as_of_date": "2026-07-22"},
        )
        self.assertIn("## 三、资产维度分析", markdown)
        self.assertIn("## 八、方案 A：协议交易与重组", markdown)
        self.assertIn("1. 先核验债权。", markdown)
        self.assertIn("- 缺少估值。", markdown)
        self.assertIn("- 【模拟假设】银行存在时间压力。", markdown)
        self.assertIn("所有非数据库确认", markdown)

    def test_assumption_disclosure_includes_type_confidence_basis_and_question(self) -> None:
        disclosures = build_assumption_disclosures(
            {
                "assumptions": [
                    {
                        "statement": "债权人可能存在季度处置压力。",
                        "kind": "industry_prior",
                        "confidence": "0.5",
                        "evidence_refs": [],
                        "verification_question": "是否存在季度考核节点？",
                    },
                    {
                        "statement": "监管方可能要求先行复工。",
                        "kind": "evidence_inference",
                        "confidence": "0.7",
                        "evidence_refs": [{"source_table": "dynamic_evidence", "record_id": "12"}],
                        "verification_question": "是否有正式监管文件？",
                    },
                ]
            }
        )
        self.assertEqual(len(disclosures), 2)
        self.assertIn("【模拟假设 H001】", disclosures[0])
        self.assertIn("类型：行业先验", disclosures[0])
        self.assertIn("置信度：0.5", disclosures[0])
        self.assertIn("依据：无直接数据库证据", disclosures[0])
        self.assertIn("核实问题：是否存在季度考核节点？", disclosures[0])
        self.assertIn("依据：dynamic_evidence#12", disclosures[1])


if __name__ == "__main__":
    unittest.main()
