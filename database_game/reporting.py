from __future__ import annotations

from typing import Any

from database_game.models import CompleteAnalysisReport


ASSUMPTION_KIND_LABELS = {
    "evidence_inference": "证据推断",
    "industry_prior": "行业先验",
    "stress_test": "压力测试",
}


def build_assumption_disclosures(value: Any) -> list[str]:
    """Extract and visibly label every structured assumption used by the final report."""

    assumptions: list[dict[str, Any]] = []
    _collect_assumptions(value, assumptions)
    disclosures: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in assumptions:
        statement = str(item.get("statement") or "").strip()
        kind = str(item.get("kind") or "").strip()
        key = (statement, kind)
        if not statement or key in seen:
            continue
        seen.add(key)
        confidence = item.get("confidence")
        question = str(item.get("verification_question") or "待线下核实").strip()
        refs = item.get("evidence_refs") or []
        evidence = "、".join(
            f"{ref.get('source_table')}#{ref.get('record_id')}"
            for ref in refs
            if isinstance(ref, dict) and ref.get("source_table") and ref.get("record_id")
        )
        basis = evidence or "无直接数据库证据"
        assumption_id = f"H{len(disclosures) + 1:03d}"
        disclosures.append(
            f"【模拟假设 {assumption_id}】类型：{ASSUMPTION_KIND_LABELS.get(kind, kind or '未分类')}；"
            f"内容：{statement}；置信度：{confidence if confidence is not None else '未给出'}；"
            f"依据：{basis}；核实问题：{question}"
        )
    return disclosures


def render_complete_report(report: CompleteAnalysisReport, public_context: dict[str, Any]) -> str:
    """Render validated report sections into stable, readable Markdown."""

    sections = [
        ("一、执行摘要", report.executive_summary),
        ("二、项目与证据范围", report.project_overview),
        ("三、资产维度分析", report.asset_analysis),
        ("四、经济财务维度分析", report.economic_analysis),
        ("五、法律与受偿顺位分析", report.legal_analysis),
        ("六、多方博弈与交易区间", report.game_analysis),
        ("七、资产确权与清算安全垫", report.liquidation_analysis),
        ("八、方案 A：协议交易与重组", report.plan_a_analysis),
        ("九、方案 B：司法强清与破产路径", report.plan_b_analysis),
        ("十、红队压力测试", report.red_team_analysis),
        ("十一、我方投资建议", report.investment_recommendation),
    ]
    project_name = str(public_context.get("project_name") or "未命名项目")
    as_of_date = str(public_context.get("as_of_date") or "未确定")
    lines = [
        f"# {report.title}",
        "",
        f"> 项目：{project_name}  ",
        f"> 数据基准日：{as_of_date}  ",
        "> 本报告由云数据库博弈链生成；事实、模拟假设与数据缺口必须分开理解。",
        "> 标记规则：所有非数据库确认的角色心理、交易底线、概率、区间和压力测试均以 `【模拟假设】` 明示，不得当作事实使用。",
    ]
    for heading, content in sections:
        lines.extend(["", f"## {heading}", "", content.strip()])
    lines.extend(["", "## 十二、下一步行动清单", ""])
    lines.extend(f"{index}. {item}" for index, item in enumerate(report.action_plan, start=1))
    lines.extend(["", "## 十三、数据缺口与模拟假设", "", "### 数据缺口", ""])
    lines.extend(_bullets(report.data_gaps, "无额外数据缺口。"))
    lines.extend(["", "### 模拟假设", ""])
    lines.extend(_bullets(report.simulated_assumptions, "无额外模拟假设。"))
    lines.extend(
        [
            "",
            "---",
            "",
            "本报告是基于当前数据库证据和测试授权形成的决策支持材料，不替代正式法律、财务、税务及资产评估意见。",
            "",
        ]
    )
    return "\n".join(lines)


def _bullets(items: list[str], empty_message: str) -> list[str]:
    return [f"- {item}" for item in items] if items else [f"- {empty_message}"]


def _collect_assumptions(value: Any, output: list[dict[str, Any]]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_assumptions(item, output)
        return
    if not isinstance(value, dict):
        return
    if {"statement", "kind", "confidence", "verification_question"}.issubset(value):
        output.append(value)
    for item in value.values():
        _collect_assumptions(item, output)
