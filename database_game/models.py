from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, str_strip_whitespace=True)


class Dimension(str, Enum):
    ASSET = "asset"
    ECONOMIC = "economic"
    LEGAL = "legal"


class FactStatus(str, Enum):
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    MISSING = "missing"


class RoleName(str, Enum):
    LIQUIDATION = "liquidation"
    DEBTOR = "debtor"
    CREDITOR = "creditor"
    REGULATOR = "regulator"


class ScenarioName(str, Enum):
    OPTIMISTIC = "optimistic"
    BASELINE = "baseline"
    ADVERSARIAL = "adversarial"


class AssumptionKind(str, Enum):
    EVIDENCE_INFERENCE = "evidence_inference"
    INDUSTRY_PRIOR = "industry_prior"
    STRESS_TEST = "stress_test"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TermKey(str, Enum):
    TOTAL_INVESTMENT = "total_investment"
    CREDITOR_RECOVERY_RATIO = "creditor_recovery_ratio"
    DEBTOR_SETTLEMENT_CASH = "debtor_settlement_cash"
    HOLDING_MONTHS = "holding_months"
    LIQUIDATION_RECOVERY_RATIO = "liquidation_recovery_ratio"
    REGULATOR_REMEDIATION_COST = "regulator_remediation_cost"


class EvidenceRef(StrictModel):
    source_table: str = Field(min_length=1, max_length=128)
    record_id: str = Field(min_length=1, max_length=128)
    locator: str | None = Field(default=None, max_length=500)
    excerpt: str | None = Field(default=None, max_length=1000)


class FactItem(StrictModel):
    fact_id: str = Field(min_length=1, max_length=128)
    dimension: Dimension
    statement: str = Field(min_length=1, max_length=2000)
    status: FactStatus
    confidence: Decimal = Field(ge=0, le=1)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    numeric_value: Decimal | None = None
    unit: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def confirmed_fact_requires_evidence(self) -> "FactItem":
        if self.status == FactStatus.CONFIRMED and not self.evidence_refs:
            raise ValueError("confirmed fact requires at least one evidence reference")
        return self


class DimensionPacket(StrictModel):
    dimension: Dimension
    confirmed_facts: list[FactItem] = Field(default_factory=list)
    disputed_items: list[FactItem] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    retrieval_report: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def facts_match_packet(self) -> "DimensionPacket":
        facts = [*self.confirmed_facts, *self.disputed_items]
        if any(fact.dimension != self.dimension for fact in facts):
            raise ValueError("fact dimension does not match packet dimension")
        if any(fact.status != FactStatus.CONFIRMED for fact in self.confirmed_facts):
            raise ValueError("confirmed_facts may only contain confirmed facts")
        if any(fact.status == FactStatus.CONFIRMED for fact in self.disputed_items):
            raise ValueError("disputed_items may not contain confirmed facts")
        return self


class PublicContext(StrictModel):
    project_id: int = Field(gt=0)
    project_name: str = Field(min_length=1, max_length=500)
    as_of_date: date
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    asset: DimensionPacket
    economic: DimensionPacket
    legal: DimensionPacket
    source_inventory: dict[str, int] = Field(default_factory=dict)
    global_missing_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def packet_slots_are_correct(self) -> "PublicContext":
        expected = {
            "asset": Dimension.ASSET,
            "economic": Dimension.ECONOMIC,
            "legal": Dimension.LEGAL,
        }
        for field_name, dimension in expected.items():
            if getattr(self, field_name).dimension != dimension:
                raise ValueError(f"{field_name} packet must use {dimension.value} dimension")
        return self


class Assumption(StrictModel):
    statement: str = Field(min_length=1, max_length=1000)
    kind: AssumptionKind
    confidence: Decimal = Field(ge=0, le=1)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    verification_question: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def evidence_inference_requires_evidence(self) -> "Assumption":
        if self.kind == AssumptionKind.EVIDENCE_INFERENCE and not self.evidence_refs:
            raise ValueError("evidence inference requires evidence references")
        return self


class TermRange(StrictModel):
    term_key: TermKey
    unit: str = Field(min_length=1, max_length=32)
    minimum: Decimal
    preferred: Decimal
    maximum: Decimal

    @model_validator(mode="after")
    def values_are_ordered(self) -> "TermRange":
        if not self.minimum <= self.preferred <= self.maximum:
            raise ValueError("term range must satisfy minimum <= preferred <= maximum")
        expected_units = {
            TermKey.TOTAL_INVESTMENT: "CNY",
            TermKey.CREDITOR_RECOVERY_RATIO: "ratio",
            TermKey.DEBTOR_SETTLEMENT_CASH: "CNY",
            TermKey.HOLDING_MONTHS: "month",
            TermKey.LIQUIDATION_RECOVERY_RATIO: "ratio",
            TermKey.REGULATOR_REMEDIATION_COST: "CNY",
        }
        if self.unit != expected_units[self.term_key]:
            raise ValueError(f"{self.term_key.value} must use {expected_units[self.term_key]} unit")
        if self.minimum < 0:
            raise ValueError("term range may not be negative")
        if self.unit == "ratio" and self.maximum > 1:
            raise ValueError("ratio term may not exceed 1")
        return self


class PrivateIncentives(StrictModel):
    role: RoleName
    scenario: ScenarioName
    kpis: list[str] = Field(min_length=1)
    hard_constraints: list[str] = Field(default_factory=list)
    forbidden_compromises: list[str] = Field(default_factory=list)
    time_pressure: Decimal = Field(ge=0, le=1)
    positions: list[TermRange] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)


class ScenarioParameters(StrictModel):
    scenario: ScenarioName
    roles: list[PrivateIncentives]

    @model_validator(mode="after")
    def contains_each_role_once(self) -> "ScenarioParameters":
        roles = [item.role for item in self.roles]
        if len(roles) != len(set(roles)) or set(roles) != set(RoleName):
            raise ValueError("scenario must contain each role exactly once")
        if any(item.scenario != self.scenario for item in self.roles):
            raise ValueError("role scenario does not match scenario container")
        return self


class ScenarioSet(StrictModel):
    scenarios: list[ScenarioParameters]

    @model_validator(mode="after")
    def contains_all_scenarios(self) -> "ScenarioSet":
        names = [item.scenario for item in self.scenarios]
        if len(names) != len(set(names)) or set(names) != set(ScenarioName):
            raise ValueError("scenario set must contain optimistic, baseline and adversarial")
        return self


class RetrievalDirective(StrictModel):
    dimension: Dimension
    objective: str = Field(min_length=1, max_length=1000)
    event_types: list[str] = Field(default_factory=list, max_length=20)
    evidence_types: list[str] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    must_verify: list[str] = Field(default_factory=list, max_length=20)
    focus_entities: list[str] = Field(default_factory=list, max_length=20)
    max_events: int = Field(default=40, ge=1, le=200)
    max_evidences: int = Field(default=50, ge=1, le=250)
    max_raw_contents: int = Field(default=8, ge=1, le=30)


class MasterRetrievalPlan(StrictModel):
    project_id: int = Field(gt=0)
    investment_thesis: str = Field(min_length=1, max_length=2000)
    directives: list[RetrievalDirective]
    cross_dimension_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def contains_three_dimensions(self) -> "MasterRetrievalPlan":
        dimensions = [item.dimension for item in self.directives]
        if len(dimensions) != len(set(dimensions)) or set(dimensions) != set(Dimension):
            raise ValueError("retrieval plan must contain asset, economic and legal directives")
        return self


class MasterMandate(StrictModel):
    investor_name: str = Field(default="我方投资人", min_length=1, max_length=200)
    positions: list[TermRange] = Field(default_factory=list)
    minimum_irr: Decimal = Field(ge=-1, le=10)
    maximum_holding_months: int = Field(gt=0, le=240)
    minimum_downside_recovery_ratio: Decimal = Field(ge=0, le=1)
    prohibited_conditions: list[str] = Field(default_factory=list)


class RoleBid(StrictModel):
    role: RoleName
    scenario: ScenarioName
    position_summary: str = Field(min_length=1, max_length=2000)
    positions: list[TermRange] = Field(default_factory=list)
    hard_conditions: list[str] = Field(default_factory=list)
    concessions: list[str] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
    tactical_moves: list[str] = Field(default_factory=list)
    cooperation_score: Decimal = Field(ge=0, le=1)
    scenario_success_score: Decimal = Field(ge=0, le=1)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)


class RoleScenarioBids(StrictModel):
    role: RoleName
    bids: list[RoleBid]

    @model_validator(mode="after")
    def contains_three_scenario_bids(self) -> "RoleScenarioBids":
        scenarios = [item.scenario for item in self.bids]
        if len(scenarios) != len(set(scenarios)) or set(scenarios) != set(ScenarioName):
            raise ValueError("role must submit one bid for each scenario")
        if any(item.role != self.role for item in self.bids):
            raise ValueError("bid role does not match role container")
        return self


class TermOverlap(StrictModel):
    term_key: str
    unit: str
    participants: list[str]
    lower_bound: Decimal
    upper_bound: Decimal
    proposed_value: Decimal | None
    gap: Decimal
    feasible: bool


class ScenarioOverlap(StrictModel):
    scenario: ScenarioName
    terms: list[TermOverlap]
    blocking_issues: list[str]
    feasible: bool


class DealBox(StrictModel):
    bids: list[RoleBid]
    overlaps: list[ScenarioOverlap]
    communication_policy: str = "角色相互隔离，只向 Deal Box 单向提交。"


class DecisionMatrixRow(StrictModel):
    party: str
    public_chips: list[str]
    simulated_private_pressure: list[Assumption]
    estimated_bottom_line: list[TermRange]
    our_countermoves: list[str]


class DecisionMatrix(StrictModel):
    rows: list[DecisionMatrixRow]
    key_overlap: str
    no_deal_triggers: list[str]


class LiquidationAsset(StrictModel):
    asset_name: str
    reference_value: Decimal | None = Field(default=None, ge=0)
    discount_rate: Decimal | None = Field(default=None, ge=0, le=1)
    liquidation_value: Decimal | None = Field(default=None, ge=0)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    discount_basis: Assumption


class LiquidationValueReport(StrictModel):
    assets: list[LiquidationAsset]
    disposal_costs: Decimal | None = Field(default=None, ge=0)
    taxes: Decimal | None = Field(default=None, ge=0)
    net_liquidation_value: Decimal | None
    recovery_waterfall: list[str]
    recognized_claims: Decimal | None = Field(default=None, gt=0)
    downside_recovery_ratio: Decimal | None = Field(default=None, ge=0, le=1)
    missing_inputs: list[str]


class PathMetrics(StrictModel):
    calculation_status: Literal["calculated", "insufficient_data"]
    total_investment: Decimal | None = Field(default=None, gt=0)
    expected_recovery: Decimal | None = Field(default=None, ge=0)
    holding_months: int | None = Field(default=None, gt=0)
    roi: Decimal | None = None
    irr: Decimal | None = None
    scenario_score: Decimal = Field(ge=0, le=1)
    calculation_basis: list[EvidenceRef] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def calculation_status_matches_inputs(self) -> "PathMetrics":
        required_values = (self.total_investment, self.expected_recovery, self.holding_months)
        if self.calculation_status == "calculated" and any(value is None for value in required_values):
            raise ValueError("calculated path requires investment, recovery and holding months")
        if self.calculation_status == "insufficient_data" and not self.missing_inputs:
            raise ValueError("insufficient_data path requires missing_inputs")
        return self


class DealPath(StrictModel):
    name: str
    structure: list[str]
    prerequisites: list[str]
    steps: list[str]
    metrics: PathMetrics
    failure_triggers: list[str]


class DynamicPaths(StrictModel):
    plan_a: DealPath
    plan_b: DealPath
    switch_triggers: list[str]


class DueDiligenceProbe(StrictModel):
    target_party: str
    question: str
    reason: str
    required_evidence: str
    priority: Severity


class RedTeamFinding(StrictModel):
    severity: Severity
    attack: str
    impact: str
    mitigation: str
    evidence_gap: str | None = None


class RedTeamAudit(StrictModel):
    findings: list[RedTeamFinding]
    actionable_probes: list[DueDiligenceProbe]
    residual_risks: list[str]


class DecisionMatrixReport(StrictModel):
    decision_matrix: DecisionMatrix
    liquidation_value: LiquidationValueReport
    dynamic_paths: DynamicPaths
    red_team_audit: RedTeamAudit


class RedTeamReview(StrictModel):
    approved: bool
    findings: list[RedTeamFinding]
    required_changes: list[str]
    actionable_probes: list[DueDiligenceProbe]


class CompleteAnalysisReport(StrictModel):
    """Human-readable report sections derived from the validated decision report."""

    title: str = Field(min_length=1, max_length=300)
    executive_summary: str = Field(min_length=1, max_length=6000)
    project_overview: str = Field(min_length=1, max_length=6000)
    asset_analysis: str = Field(min_length=1, max_length=8000)
    economic_analysis: str = Field(min_length=1, max_length=8000)
    legal_analysis: str = Field(min_length=1, max_length=8000)
    game_analysis: str = Field(min_length=1, max_length=8000)
    liquidation_analysis: str = Field(min_length=1, max_length=8000)
    plan_a_analysis: str = Field(min_length=1, max_length=8000)
    plan_b_analysis: str = Field(min_length=1, max_length=8000)
    red_team_analysis: str = Field(min_length=1, max_length=8000)
    investment_recommendation: str = Field(min_length=1, max_length=6000)
    action_plan: list[str] = Field(min_length=1, max_length=30)
    data_gaps: list[str] = Field(default_factory=list, max_length=50)
    simulated_assumptions: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def add_missing_assumption_labels(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        narrative_fields = (
            "executive_summary",
            "project_overview",
            "asset_analysis",
            "economic_analysis",
            "legal_analysis",
            "game_analysis",
            "liquidation_analysis",
            "plan_a_analysis",
            "plan_b_analysis",
            "red_team_analysis",
            "investment_recommendation",
        )
        for field_name in narrative_fields:
            content = normalized.get(field_name)
            if isinstance(content, str):
                normalized[field_name] = _label_assumption_sentences(content)
        assumptions = normalized.get("simulated_assumptions")
        if isinstance(assumptions, list):
            normalized["simulated_assumptions"] = [
                item
                if not isinstance(item, str) or re.match(r"^【模拟假设(?: H\d{3})?】", item.strip())
                else f"【模拟假设】{item.strip()}"
                for item in assumptions
            ]
        return normalized

    @model_validator(mode="after")
    def simulated_assumptions_are_visibly_labeled(self) -> "CompleteAnalysisReport":
        label_pattern = r"^【模拟假设(?: H\d{3})?】"
        if any(not re.match(label_pattern, item.strip()) for item in self.simulated_assumptions):
            raise ValueError("every simulated assumption must start with 【模拟假设】")
        narrative_fields = (
            self.executive_summary,
            self.project_overview,
            self.asset_analysis,
            self.economic_analysis,
            self.legal_analysis,
            self.game_analysis,
            self.liquidation_analysis,
            self.plan_a_analysis,
            self.plan_b_analysis,
            self.red_team_analysis,
            self.investment_recommendation,
        )
        for content in narrative_fields:
            without_labels = re.sub(r"【模拟假设(?: H\d{3})?】", "", content)
            if "模拟假设" in without_labels:
                raise ValueError("simulated assumptions in narrative must use the visible label")
        return self


def _label_assumption_sentences(content: str) -> str:
    markers = ("模拟假设", "行业先验", "压力测试", "置信度")
    quantified_uncertainty = re.compile(
        r"(?:可能|预计|估计|通常|大概率|或将|假定|暂按|参考).*(?:\d+(?:\.\d+)?%|\d+(?:\.\d+)?(?:万|亿)?元|\d+(?:\.\d+)?(?:个)?月|\d+(?:\.\d+)?年)"
    )
    parts = re.split(r"(?<=[。；\n])", content)
    labeled: list[str] = []
    for part in parts:
        needs_label = any(marker in part for marker in markers) or bool(quantified_uncertainty.search(part))
        if needs_label and "【模拟假设" not in part:
            leading = part[: len(part) - len(part.lstrip())]
            body = part.lstrip().replace("模拟假设", "非事实推演")
            part = f"{leading}【模拟假设】{body}"
        labeled.append(part)
    return "".join(labeled)
