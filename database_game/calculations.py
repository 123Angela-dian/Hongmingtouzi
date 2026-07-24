from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, getcontext
from typing import Iterable

from database_game.models import (
    DecisionMatrixReport,
    DealBox,
    MasterMandate,
    RedTeamFinding,
    RoleBid,
    ScenarioName,
    ScenarioOverlap,
    TermOverlap,
    TermRange,
    Severity,
)


getcontext().prec = 28


def calculate_deal_box(mandate: MasterMandate, bids: Iterable[RoleBid]) -> DealBox:
    bid_list = list(bids)
    overlaps = [
        _calculate_scenario_overlap(
            scenario,
            mandate.positions,
            [bid for bid in bid_list if bid.scenario == scenario],
        )
        for scenario in ScenarioName
    ]
    return DealBox(bids=bid_list, overlaps=overlaps)


def _calculate_scenario_overlap(
    scenario: ScenarioName,
    mandate_positions: list[TermRange],
    bids: list[RoleBid],
) -> ScenarioOverlap:
    grouped: dict[str, list[tuple[str, TermRange]]] = defaultdict(list)
    for position in mandate_positions:
        grouped[position.term_key.value].append(("investor", position))
    for bid in bids:
        for position in bid.positions:
            grouped[position.term_key.value].append((bid.role.value, position))

    terms: list[TermOverlap] = []
    unit_conflicts: list[str] = []
    for term_key, entries in sorted(grouped.items()):
        units = {position.unit for _, position in entries}
        if len(units) != 1:
            unit_conflicts.append(f"{term_key} 的单位不一致：{sorted(units)}")
            continue
        lower = max(position.minimum for _, position in entries)
        upper = min(position.maximum for _, position in entries)
        feasible = lower <= upper
        gap = Decimal("0") if feasible else lower - upper
        terms.append(
            TermOverlap(
                term_key=term_key,
                unit=next(iter(units)),
                participants=[participant for participant, _ in entries],
                lower_bound=lower,
                upper_bound=upper,
                proposed_value=(lower + upper) / Decimal("2") if feasible else None,
                gap=gap,
                feasible=feasible,
            )
        )

    blockers = [issue for bid in bids for issue in bid.blocking_issues]
    blockers.extend(unit_conflicts)
    return ScenarioOverlap(
        scenario=scenario,
        terms=terms,
        blocking_issues=list(dict.fromkeys(blockers)),
        feasible=all(term.feasible for term in terms) and not blockers,
    )


def calculate_roi(total_investment: Decimal, expected_recovery: Decimal) -> Decimal:
    if total_investment <= 0:
        raise ValueError("total investment must be positive")
    return (expected_recovery - total_investment) / total_investment


def calculate_annualized_irr(
    cash_flows: list[Decimal],
    *,
    periods_per_year: int = 12,
    tolerance: Decimal = Decimal("0.0000001"),
    max_iterations: int = 300,
) -> Decimal | None:
    if len(cash_flows) < 2 or not any(value < 0 for value in cash_flows) or not any(value > 0 for value in cash_flows):
        return None

    low = Decimal("-0.9999")
    high = Decimal("10")

    def npv(periodic_rate: Decimal) -> Decimal:
        return sum(value / ((Decimal("1") + periodic_rate) ** index) for index, value in enumerate(cash_flows))

    low_value = npv(low)
    high_value = npv(high)
    if low_value * high_value > 0:
        return None

    for _ in range(max_iterations):
        middle = (low + high) / Decimal("2")
        middle_value = npv(middle)
        if abs(middle_value) <= tolerance:
            periodic_rate = middle
            break
        if low_value * middle_value <= 0:
            high = middle
        else:
            low = middle
            low_value = middle_value
    else:
        periodic_rate = (low + high) / Decimal("2")

    return (Decimal("1") + periodic_rate) ** periods_per_year - Decimal("1")


def discounted_liquidation_value(
    reference_value: Decimal,
    discount_rate: Decimal,
    disposal_costs: Decimal = Decimal("0"),
    taxes: Decimal = Decimal("0"),
) -> Decimal:
    if reference_value < 0 or disposal_costs < 0 or taxes < 0:
        raise ValueError("liquidation inputs may not be negative")
    if not Decimal("0") <= discount_rate <= Decimal("1"):
        raise ValueError("discount rate must be between 0 and 1")
    return reference_value * (Decimal("1") - discount_rate) - disposal_costs - taxes


def normalize_report_calculations(report: DecisionMatrixReport) -> DecisionMatrixReport:
    liquidation_assets = []
    for asset in report.liquidation_value.assets:
        liquidation_value = None
        if asset.reference_value is not None and asset.discount_rate is not None:
            liquidation_value = discounted_liquidation_value(
                asset.reference_value,
                asset.discount_rate,
            )
        liquidation_assets.append(
            asset.model_copy(
                update={"liquidation_value": liquidation_value}
            )
        )
    net_liquidation_value = None
    disposal_costs = report.liquidation_value.disposal_costs
    taxes = report.liquidation_value.taxes
    asset_values = [asset.liquidation_value for asset in liquidation_assets]
    if liquidation_assets and all(value is not None for value in asset_values) and disposal_costs is not None and taxes is not None:
        net_liquidation_value = (
            sum((value for value in asset_values if value is not None), Decimal("0"))
            - disposal_costs
            - taxes
        )
    recognized_claims = report.liquidation_value.recognized_claims
    downside_ratio = None
    if recognized_claims and recognized_claims > 0 and net_liquidation_value is not None:
        raw_ratio = net_liquidation_value / recognized_claims
        downside_ratio = min(Decimal("1"), max(Decimal("0"), raw_ratio))
    liquidation = report.liquidation_value.model_copy(
        update={
            "assets": liquidation_assets,
            "net_liquidation_value": net_liquidation_value,
            "downside_recovery_ratio": downside_ratio,
        }
    )

    def normalize_path(path):
        metrics = path.metrics
        if metrics.calculation_status != "calculated":
            return path.model_copy(update={"metrics": metrics.model_copy(update={"roi": None, "irr": None})})
        assert metrics.total_investment is not None
        assert metrics.expected_recovery is not None
        assert metrics.holding_months is not None
        cash_flows = [Decimal("0")] * (metrics.holding_months + 1)
        cash_flows[0] = -metrics.total_investment
        cash_flows[-1] = metrics.expected_recovery
        normalized_metrics = metrics.model_copy(
            update={
                "roi": calculate_roi(metrics.total_investment, metrics.expected_recovery),
                "irr": calculate_annualized_irr(cash_flows),
            }
        )
        return path.model_copy(update={"metrics": normalized_metrics})

    dynamic_paths = report.dynamic_paths.model_copy(
        update={
            "plan_a": normalize_path(report.dynamic_paths.plan_a),
            "plan_b": normalize_path(report.dynamic_paths.plan_b),
        }
    )
    return report.model_copy(update={"liquidation_value": liquidation, "dynamic_paths": dynamic_paths})


def apply_mandate_guardrails(
    report: DecisionMatrixReport,
    mandate: MasterMandate,
) -> DecisionMatrixReport:
    findings = list(report.red_team_audit.findings)
    no_deal_triggers = list(report.decision_matrix.no_deal_triggers)

    def add_finding(severity: Severity, attack: str, impact: str, mitigation: str, gap: str | None = None) -> None:
        if any(item.attack == attack for item in findings):
            return
        findings.append(
            RedTeamFinding(
                severity=severity,
                attack=attack,
                impact=impact,
                mitigation=mitigation,
                evidence_gap=gap,
            )
        )

    for path in (report.dynamic_paths.plan_a, report.dynamic_paths.plan_b):
        metrics = path.metrics
        if metrics.calculation_status != "calculated":
            add_finding(
                Severity.HIGH,
                f"{path.name}缺少可计算现金流",
                "无法验证是否满足我方收益授权。",
                "补齐投入、回收和周期后重新运行。",
                "、".join(metrics.missing_inputs),
            )
            continue
        if metrics.irr is not None and metrics.irr < mandate.minimum_irr:
            trigger = f"{path.name} IRR 低于我方最低要求"
            no_deal_triggers.append(trigger)
            add_finding(
                Severity.HIGH,
                trigger,
                "方案不满足投资授权。",
                "压低投入、提高确定性回收或缩短持有期。",
            )
        if metrics.holding_months and metrics.holding_months > mandate.maximum_holding_months:
            trigger = f"{path.name}持有期超过我方上限"
            no_deal_triggers.append(trigger)
            add_finding(
                Severity.HIGH,
                trigger,
                "资金占用超过投资授权。",
                "设置阶段退出节点或否决该路径。",
            )

    downside_ratio = report.liquidation_value.downside_recovery_ratio
    if downside_ratio is None:
        add_finding(
            Severity.HIGH,
            "清算覆盖率无法计算",
            "无法确认最坏情形安全垫。",
            "补齐有效债权总额、资产参考价值、折扣、税费和处置成本。",
            "清算计算输入不完整",
        )
    elif downside_ratio < mandate.minimum_downside_recovery_ratio:
        trigger = "清算回收率低于我方最低安全垫"
        no_deal_triggers.append(trigger)
        add_finding(
            Severity.CRITICAL,
            trigger,
            "司法退出无法覆盖投资授权要求。",
            "降低收购成本、增加增信或否决交易。",
        )

    audit = report.red_team_audit.model_copy(update={"findings": findings})
    matrix = report.decision_matrix.model_copy(update={"no_deal_triggers": list(dict.fromkeys(no_deal_triggers))})
    return report.model_copy(update={"decision_matrix": matrix, "red_team_audit": audit})
