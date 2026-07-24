from __future__ import annotations

import json
import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from database_game.calculations import apply_mandate_guardrails, calculate_deal_box, normalize_report_calculations
from database_game.context import build_public_context
from database_game.gateway import StructuredModelGateway
from database_game.models import (
    CompleteAnalysisReport,
    DecisionMatrixReport,
    Dimension,
    DimensionPacket,
    MasterMandate,
    MasterRetrievalPlan,
    RedTeamReview,
    RoleName,
    RoleScenarioBids,
    ScenarioSet,
)
from database_game.prompts import (
    COMPLETE_REPORT_PROMPT,
    FACT_ENGINE_PROMPTS,
    MASTER_FINAL_PROMPT,
    MASTER_RETRIEVAL_PROMPT,
    MASTER_SYNTHESIS_PROMPT,
    RED_TEAM_PROMPT,
    ROLE_PROMPTS,
    SCENARIO_PARAMETER_PROMPT,
)
from database_game.repository import CloudDatabaseRepository, GameRepository
from database_game.reporting import build_assumption_disclosures, render_complete_report


class DatabaseGameState(TypedDict, total=False):
    project_id: int
    mandate: dict[str, Any]
    planning_snapshot: dict[str, Any]
    master_plan: dict[str, Any]
    fact_packets: Annotated[list[dict[str, Any]], operator.add]
    public_context: dict[str, Any]
    scenario_set: dict[str, Any]
    role_bid_sets: Annotated[list[dict[str, Any]], operator.add]
    deal_box: dict[str, Any]
    draft_report: dict[str, Any]
    red_team_review: dict[str, Any]
    final_report: dict[str, Any]
    complete_report: dict[str, Any]
    complete_report_markdown: str


DIMENSION_AGENT_NAMES = {
    Dimension.ASSET: ("asset_agent",),
    Dimension.ECONOMIC: ("economic_agent", "financial_agent"),
    Dimension.LEGAL: ("legal_agent",),
}


def _json_payload(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class DatabaseGameEngine:
    def __init__(
        self,
        gateway: StructuredModelGateway,
        env_path: Path = Path(".env"),
        repository: GameRepository | None = None,
    ):
        self.gateway = gateway
        self.repository = repository or CloudDatabaseRepository(env_path)
        self.compiled_graph = self._build_graph()

    def run(self, project_id: int, mandate: MasterMandate) -> DecisionMatrixReport:
        state = self.run_state(project_id, mandate)
        return DecisionMatrixReport.model_validate(state["final_report"])

    def run_state(self, project_id: int, mandate: MasterMandate) -> DatabaseGameState:
        return self.compiled_graph.invoke(
            {
                "project_id": project_id,
                "mandate": mandate.model_dump(mode="json"),
                "fact_packets": [],
                "role_bid_sets": [],
            },
            config={
                "run_name": "database_game_master",
                "metadata": {"project_id": project_id, "source": "cloud_mysql_game"},
            },
        )

    def _build_graph(self):
        graph = StateGraph(DatabaseGameState)
        graph.add_node("master_retrieval_plan", self._master_retrieval_plan)
        for dimension in Dimension:
            graph.add_node(f"{dimension.value}_fact_engine", self._fact_node(dimension))
        graph.add_node("build_public_context", self._build_public_context)
        graph.add_node("build_scenarios", self._build_scenarios)
        for role in RoleName:
            graph.add_node(f"{role.value}_sandbox", self._role_node(role))
        graph.add_node("deal_box", self._deal_box)
        graph.add_node("master_synthesis", self._master_synthesis)
        graph.add_node("red_team", self._red_team)
        graph.add_node("master_final", self._master_final)
        graph.add_node("complete_analysis_report", self._complete_analysis_report)

        graph.add_edge(START, "master_retrieval_plan")
        fact_nodes = [f"{dimension.value}_fact_engine" for dimension in Dimension]
        for node in fact_nodes:
            graph.add_edge("master_retrieval_plan", node)
        graph.add_edge(fact_nodes, "build_public_context")
        graph.add_edge("build_public_context", "build_scenarios")
        role_nodes = [f"{role.value}_sandbox" for role in RoleName]
        for node in role_nodes:
            graph.add_edge("build_scenarios", node)
        graph.add_edge(role_nodes, "deal_box")
        graph.add_edge("deal_box", "master_synthesis")
        graph.add_edge("master_synthesis", "red_team")
        graph.add_edge("red_team", "master_final")
        graph.add_edge("master_final", "complete_analysis_report")
        graph.add_edge("complete_analysis_report", END)
        return graph.compile()

    def _master_retrieval_plan(self, state: DatabaseGameState) -> DatabaseGameState:
        snapshot = self.repository.planning_snapshot(state["project_id"])
        plan = self.gateway.invoke(
            MasterRetrievalPlan,
            MASTER_RETRIEVAL_PROMPT,
            _json_payload(snapshot),
        )
        if plan.project_id != state["project_id"]:
            plan = plan.model_copy(update={"project_id": state["project_id"]})
        return {
            "planning_snapshot": snapshot,
            "master_plan": plan.model_dump(mode="json"),
        }

    def _fact_node(self, dimension: Dimension):
        def run(state: DatabaseGameState) -> DatabaseGameState:
            plan = MasterRetrievalPlan.model_validate(state["master_plan"])
            directive = next(item for item in plan.directives if item.dimension == dimension)
            contexts: list[str] = []
            reports: list[dict[str, Any]] = []
            for agent_name in DIMENSION_AGENT_NAMES[dimension]:
                result = self.repository.retrieve_context(
                    state["project_id"],
                    agent_name,
                    directive.model_dump(mode="json"),
                )
                contexts.extend(result.get("sections", {}).values())
                reports.append(result.get("report") or {})
            packet = self.gateway.invoke(
                DimensionPacket,
                FACT_ENGINE_PROMPTS[dimension],
                _json_payload(
                    {
                        "dimension": dimension.value,
                        "master_directive": directive,
                        "retrieved_context": contexts,
                    }
                ),
            )
            if packet.dimension != dimension:
                raise ValueError(f"fact engine returned {packet.dimension.value}, expected {dimension.value}")
            packet = packet.model_copy(update={"retrieval_report": {"runs": reports}})
            return {"fact_packets": [packet.model_dump(mode="json")]}

        return run

    def _build_public_context(self, state: DatabaseGameState) -> DatabaseGameState:
        snapshot = state["planning_snapshot"]
        inventory = snapshot["inventory"]
        context = build_public_context(
            project_id=state["project_id"],
            project_name=inventory["project_name"],
            source_inventory=inventory.get("counts") or {},
            packets=[DimensionPacket.model_validate(item) for item in state["fact_packets"]],
        )
        return {"public_context": context.model_dump(mode="json")}

    def _build_scenarios(self, state: DatabaseGameState) -> DatabaseGameState:
        scenario_set = self.gateway.invoke(
            ScenarioSet,
            SCENARIO_PARAMETER_PROMPT,
            _json_payload(
                {
                    "public_context": state["public_context"],
                    "investor_mandate": state["mandate"],
                }
            ),
        )
        return {"scenario_set": scenario_set.model_dump(mode="json")}

    def _role_node(self, role: RoleName):
        def run(state: DatabaseGameState) -> DatabaseGameState:
            scenario_set = ScenarioSet.model_validate(state["scenario_set"])
            private_states = [
                next(item for item in scenario.roles if item.role == role)
                for scenario in scenario_set.scenarios
            ]
            bids = self.gateway.invoke(
                RoleScenarioBids,
                ROLE_PROMPTS[role],
                _json_payload(
                    {
                        "public_context": state["public_context"],
                        "private_incentives": private_states,
                        "submission_rules": {
                            "recipient": "Deal Box only",
                            "absolute_blockers_only": True,
                            "probabilities_are_scenario_scores": True,
                        },
                    }
                ),
            )
            if bids.role != role:
                raise ValueError(f"sandbox returned {bids.role.value}, expected {role.value}")
            return {"role_bid_sets": [bids.model_dump(mode="json")]}

        return run

    def _deal_box(self, state: DatabaseGameState) -> DatabaseGameState:
        bid_sets = [RoleScenarioBids.model_validate(item) for item in state["role_bid_sets"]]
        bids = [bid for bid_set in bid_sets for bid in bid_set.bids]
        deal_box = calculate_deal_box(MasterMandate.model_validate(state["mandate"]), bids)
        return {"deal_box": deal_box.model_dump(mode="json")}

    def _master_synthesis(self, state: DatabaseGameState) -> DatabaseGameState:
        report = self.gateway.invoke(
            DecisionMatrixReport,
            MASTER_SYNTHESIS_PROMPT,
            _json_payload(
                {
                    "public_context": state["public_context"],
                    "investor_mandate": state["mandate"],
                    "deal_box": state["deal_box"],
                    "calculation_rule": "ROI、IRR、清算净值会由 Python 覆盖重算",
                }
            ),
        )
        return {"draft_report": normalize_report_calculations(report).model_dump(mode="json")}

    def _red_team(self, state: DatabaseGameState) -> DatabaseGameState:
        review = self.gateway.invoke(
            RedTeamReview,
            RED_TEAM_PROMPT,
            _json_payload(
                {
                    "public_context": state["public_context"],
                    "deal_box": state["deal_box"],
                    "draft_report": state["draft_report"],
                }
            ),
        )
        return {"red_team_review": review.model_dump(mode="json")}

    def _master_final(self, state: DatabaseGameState) -> DatabaseGameState:
        final_report = self.gateway.invoke(
            DecisionMatrixReport,
            MASTER_FINAL_PROMPT,
            _json_payload(
                {
                    "draft_report": state["draft_report"],
                    "red_team_review": state["red_team_review"],
                }
            ),
        )
        normalized = normalize_report_calculations(final_report)
        guarded = apply_mandate_guardrails(normalized, MasterMandate.model_validate(state["mandate"]))
        return {"final_report": guarded.model_dump(mode="json")}

    def _complete_analysis_report(self, state: DatabaseGameState) -> DatabaseGameState:
        assumption_disclosures = build_assumption_disclosures(state["final_report"])
        report = self.gateway.invoke(
            CompleteAnalysisReport,
            COMPLETE_REPORT_PROMPT,
            _json_payload(
                {
                    "public_context": state["public_context"],
                    "investor_mandate": state["mandate"],
                    "final_decision_report": state["final_report"],
                    "approved_simulated_assumptions": assumption_disclosures,
                }
            ),
        )
        report = report.model_copy(update={"simulated_assumptions": assumption_disclosures})
        markdown = render_complete_report(report, state["public_context"])
        return {
            "complete_report": report.model_dump(mode="json"),
            "complete_report_markdown": markdown,
        }
