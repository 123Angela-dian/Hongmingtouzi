from __future__ import annotations

import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from database_game.calculations import normalize_report_calculations
from database_game.graph import DatabaseGameEngine
from database_game.models import (
    Assumption,
    CompleteAnalysisReport,
    DecisionMatrix,
    DecisionMatrixReport,
    DealPath,
    Dimension,
    DimensionPacket,
    DynamicPaths,
    EvidenceRef,
    FactItem,
    FactStatus,
    LiquidationValueReport,
    LiquidationAsset,
    MasterMandate,
    MasterRetrievalPlan,
    PathMetrics,
    PrivateIncentives,
    RedTeamAudit,
    RedTeamReview,
    RetrievalDirective,
    RoleBid,
    RoleName,
    RoleScenarioBids,
    ScenarioName,
    ScenarioParameters,
    ScenarioSet,
)
from database_game.prompts import FACT_ENGINE_PROMPTS, ROLE_PROMPTS


class FakeRepository:
    def planning_snapshot(self, project_id: int):
        return {
            "inventory": {"project_id": project_id, "project_name": "测试项目", "counts": {"events": 3}},
            "available_event_types": {"mortgage_or_pledge": 1},
            "available_evidence_types": {"asset": 1},
            "fact_snapshot": [],
            "risk_snapshot": [],
            "entity_snapshot": [],
        }

    def retrieve_context(self, project_id: int, agent_name: str, retrieval_plan: dict):
        return {
            "sections": {"context": f"project={project_id}; agent={agent_name}"},
            "report": {"agent": agent_name, "master_directive_applied": retrieval_plan},
        }


class FakeGateway:
    def invoke(self, output_model, system_prompt: str, user_payload: str):
        if output_model is MasterRetrievalPlan:
            return MasterRetrievalPlan(
                project_id=1,
                investment_thesis="验证数据库博弈流程。",
                directives=[
                    RetrievalDirective(dimension=dimension, objective=f"核验{dimension.value}")
                    for dimension in Dimension
                ],
            )
        if output_model is DimensionPacket:
            dimension = next(item for item, prompt in FACT_ENGINE_PROMPTS.items() if prompt == system_prompt)
            return DimensionPacket(
                dimension=dimension,
                confirmed_facts=[
                    FactItem(
                        fact_id=f"{dimension.value}-1",
                        dimension=dimension,
                        statement=f"{dimension.value} confirmed fact",
                        status=FactStatus.CONFIRMED,
                        confidence=Decimal("0.9"),
                        evidence_refs=[EvidenceRef(source_table="evidences", record_id="1")],
                    )
                ],
            )
        if output_model is ScenarioSet:
            return ScenarioSet(
                scenarios=[
                    ScenarioParameters(
                        scenario=scenario,
                        roles=[
                            PrivateIncentives(
                                role=role,
                                scenario=scenario,
                                kpis=["执行角色目标"],
                                time_pressure=Decimal("0.5"),
                            )
                            for role in RoleName
                        ],
                    )
                    for scenario in ScenarioName
                ]
            )
        if output_model is RoleScenarioBids:
            role = next(item for item, prompt in ROLE_PROMPTS.items() if prompt == system_prompt)
            return RoleScenarioBids(
                role=role,
                bids=[
                    RoleBid(
                        role=role,
                        scenario=scenario,
                        position_summary=f"{role.value} bid",
                        cooperation_score=Decimal("0.5"),
                        scenario_success_score=Decimal("0.5"),
                    )
                    for scenario in ScenarioName
                ],
            )
        if output_model is RedTeamReview:
            return RedTeamReview(approved=True, findings=[], required_changes=[], actionable_probes=[])
        if output_model is DecisionMatrixReport:
            return _report_fixture()
        if output_model is CompleteAnalysisReport:
            return _complete_report_fixture()
        raise AssertionError(output_model)


def _report_fixture() -> DecisionMatrixReport:
    def path(name: str) -> DealPath:
        return DealPath(
            name=name,
            structure=["测试结构"],
            prerequisites=[],
            steps=["执行"],
            metrics=PathMetrics(
                calculation_status="calculated",
                total_investment=Decimal("100"),
                expected_recovery=Decimal("121"),
                holding_months=12,
                roi=Decimal("0"),
                irr=None,
                scenario_score=Decimal("0.5"),
            ),
            failure_triggers=[],
        )

    return DecisionMatrixReport(
        decision_matrix=DecisionMatrix(rows=[], key_overlap="测试交集", no_deal_triggers=[]),
        liquidation_value=LiquidationValueReport(
            assets=[],
            disposal_costs=Decimal("0"),
            taxes=Decimal("0"),
            net_liquidation_value=None,
            recovery_waterfall=[],
            missing_inputs=[],
        ),
        dynamic_paths=DynamicPaths(plan_a=path("方案A"), plan_b=path("方案B"), switch_triggers=[]),
        red_team_audit=RedTeamAudit(findings=[], actionable_probes=[], residual_risks=[]),
    )


def _complete_report_fixture() -> CompleteAnalysisReport:
    return CompleteAnalysisReport(
        title="测试项目完整分析报告",
        executive_summary="建议附条件推进。",
        project_overview="当前数据库证据覆盖资产、经济财务和法律维度。",
        asset_analysis="资产证据分析。",
        economic_analysis="经济财务证据分析。",
        legal_analysis="法律证据分析。",
        game_analysis="角色博弈分析。",
        liquidation_analysis="清算安全垫分析。",
        plan_a_analysis="协议交易路径分析。",
        plan_b_analysis="司法强清路径分析。",
        red_team_analysis="红队风险分析。",
        investment_recommendation="满足前置条件后再进入交易。",
        action_plan=["核验第一顺位债权。"],
        data_gaps=["地块估值。"],
        simulated_assumptions=["【模拟假设】债权人存在处置时间压力。"],
    )


class DatabaseGameGraphTest(unittest.TestCase):
    @patch.dict(
        os.environ,
        {"LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"},
        clear=False,
    )
    def test_graph_runs_end_to_end_with_isolated_dependencies(self) -> None:
        engine = DatabaseGameEngine(FakeGateway(), repository=FakeRepository())
        mandate = MasterMandate(
            minimum_irr=Decimal("0.15"),
            maximum_holding_months=36,
            minimum_downside_recovery_ratio=Decimal("0.70"),
        )
        state = engine.run_state(1, mandate)
        report = DecisionMatrixReport.model_validate(state["final_report"])
        self.assertIsNone(report.liquidation_value.net_liquidation_value)
        self.assertEqual(report.dynamic_paths.plan_a.metrics.roi, Decimal("0.21"))
        self.assertAlmostEqual(float(report.dynamic_paths.plan_a.metrics.irr or 0), 0.21, places=5)
        self.assertIn("# 测试项目完整分析报告", state["complete_report_markdown"])
        self.assertIn("## 十三、数据缺口与模拟假设", state["complete_report_markdown"])

    def test_missing_liquidation_values_remain_unknown(self) -> None:
        report = _report_fixture()
        unknown_asset = LiquidationAsset(
            asset_name="缺少估值的地块",
            reference_value=None,
            discount_rate=None,
            liquidation_value=None,
            evidence_refs=[EvidenceRef(source_table="dynamic_evidence", record_id="1")],
            discount_basis=Assumption(
                statement="尚未取得评估报告。",
                kind="industry_prior",
                confidence=Decimal("0.1"),
                verification_question="最新评估价值是多少？",
            ),
        )
        liquidation = report.liquidation_value.model_copy(
            update={
                "assets": [unknown_asset],
                "disposal_costs": None,
                "taxes": None,
            }
        )
        normalized = normalize_report_calculations(report.model_copy(update={"liquidation_value": liquidation}))
        self.assertIsNone(normalized.liquidation_value.assets[0].liquidation_value)
        self.assertIsNone(normalized.liquidation_value.net_liquidation_value)
        self.assertIsNone(normalized.liquidation_value.downside_recovery_ratio)


if __name__ == "__main__":
    unittest.main()
