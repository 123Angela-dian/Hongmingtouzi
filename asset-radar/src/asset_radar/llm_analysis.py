from __future__ import annotations

import json

from .llm_client import ChatMessage, CherryINClient, CherryINConfigError, extract_text
from .model_config import model_for_node
from .models import CoarseFilterResult, RawNotice, ScreeningScore
from .rules import coarse_filter as rule_coarse_filter, detailed_screening as rule_detailed_screening


def _safe_json(text: str) -> dict:
    cleaned = text.strip()
    if "```" in cleaned:
        cleaned = cleaned.replace("```json", "```")
        parts = cleaned.split("```")
        cleaned = max(parts, key=len).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start:end + 1]
    return json.loads(cleaned)


def analyze_notice_with_llm(notice: RawNotice) -> dict:
    """Deep extraction using the configured LLM, with deterministic fallback upstream."""
    profile = model_for_node("detailed_screening_node")
    text = f"标题：{notice.title}\n来源：{notice.source_platform}\n城市：{notice.city}\n金额：{notice.amount_text}\n正文：{notice.detail_text or notice.raw_text}\n附件：{json.dumps(notice.attachments, ensure_ascii=False)[:3000]}"
    prompt = """
你是困境资产/法拍/AMC公告分析助手。请只返回 JSON，不要 Markdown。
字段：
{
  "summary": "100字以内项目概述",
  "collateral_detail": "抵质押物/权益/标的资产的具体描述",
  "debtor": "债务人或被执行人，没有则空字符串",
  "city": "城市，没有则空字符串",
  "amount_text": "金额/起拍价/债权本金，没有则空字符串",
  "asset_types": ["房产/土地/股权/收费权等"],
  "risk_tags": ["查封/抵押/瑕疵/租赁/破产等"],
  "score": 0到100整数,
  "pool": "scan/initial/trackable/review/archive之一",
  "reasons": ["评分理由"]
}
分池规则：仅扫描到但信息不足为 scan；有明确资产/债权处置线索为 initial；70分以上 trackable；50-69 review；明显无价值 archive。
"""
    client = CherryINClient()
    response = client.chat(
        profile,
        [ChatMessage(role="system", content=prompt), ChatMessage(role="user", content=text[:12000])],
        max_tokens=1200,
        temperature=0,
    )
    data = _safe_json(extract_text(response))
    data["model_name"] = profile.model
    return data


def _fallback_detail_sections(notice: RawNotice, analysis: dict | None = None) -> list[dict[str, str]]:
    analysis = analysis or {}
    text = (notice.detail_text or notice.raw_text or notice.title).strip()
    summary = str(analysis.get("summary") or notice.metadata.get("source_scan_summary") or notice.title)
    collateral = str(analysis.get("collateral_detail") or "")
    amount = str(analysis.get("amount_text") or notice.amount_text or "")
    risk_tags = analysis.get("risk_tags") if isinstance(analysis.get("risk_tags"), list) else []
    asset_types = analysis.get("asset_types") if isinstance(analysis.get("asset_types"), list) else []
    sections = [
        {"title": "项目概览", "content": summary[:500]},
        {"title": "标的/抵质押物", "content": collateral[:700] or "公告未直接披露清晰的抵质押物或标的资产，需要继续查看原文和附件。"},
        {"title": "金额与处置方式", "content": amount[:300] or "公告未提取到明确金额；后续可从附件、拍卖页或补充公告继续核验。"},
        {"title": "资产类型", "content": "、".join(str(x) for x in asset_types) or "待进一步识别"},
        {"title": "风险提示", "content": "、".join(str(x) for x in risk_tags) or "暂无明确风险标签，仍需关注查封、租赁、欠费、权属瑕疵等信息。"},
        {"title": "原文线索", "content": text[:900] if text else "暂无公告正文。"},
    ]
    return [section for section in sections if section["content"]]


def structure_notice_detail_with_llm(notice: RawNotice, analysis: dict | None = None) -> list[dict[str, str]]:
    profile = model_for_node("detail_readability_node")
    text = f"""
标题：{notice.title}
来源：{notice.source_platform}
城市：{notice.city}
金额：{notice.amount_text}
摘要：{(analysis or {}).get("summary", "")}
抵质押物/权益：{(analysis or {}).get("collateral_detail", "")}
正文：{notice.detail_text or notice.raw_text}
附件：{json.dumps(notice.attachments, ensure_ascii=False)[:2000]}
""".strip()
    prompt = """
你是困境资产公告整理助手。请把公告正文整理成面向前端用户阅读的结构化内容。
只返回 JSON，不要 Markdown。
格式：
{
  "sections": [
    {"title": "项目概览", "content": "用清楚短段落说明项目是什么"},
    {"title": "标的/抵质押物", "content": "列明不动产、土地、股权、收费权、应收账款等"},
    {"title": "金额与处置方式", "content": "金额、起拍价、债权本金、转让/拍卖/招商方式"},
    {"title": "处置进展", "content": "公告、挂牌、拍卖轮次、报名/竞价时间等"},
    {"title": "风险提示", "content": "查封、抵押、租赁、破产、欠费、权属瑕疵等，没有就写待核验"}
  ]
}
要求：每段不超过220字；不要编造公告没有的信息；缺失信息写“未披露/待核验”。
"""
    try:
        client = CherryINClient()
        response = client.chat(
            profile,
            [ChatMessage(role="system", content=prompt), ChatMessage(role="user", content=text[:10000])],
            max_tokens=1300,
            temperature=0,
        )
        data = _safe_json(extract_text(response))
        sections = data.get("sections")
        if not isinstance(sections, list):
            raise ValueError("sections missing")
        cleaned: list[dict[str, str]] = []
        for item in sections[:8]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            if title and content:
                cleaned.append({"title": title[:40], "content": content[:900]})
        if cleaned:
            return cleaned
    except (CherryINConfigError, Exception):
        pass
    return _fallback_detail_sections(notice, analysis)


def llm_or_rule_analysis(notice: RawNotice) -> tuple[CoarseFilterResult, ScreeningScore, dict]:
    rule_coarse = rule_coarse_filter(notice)
    rule_screen = rule_detailed_screening(notice)
    try:
        data = analyze_notice_with_llm(notice)
    except (CherryINConfigError, Exception) as exc:
        return rule_coarse, rule_screen, {"llm_used": False, "llm_error": str(exc)}

    asset_types = data.get("asset_types") if isinstance(data.get("asset_types"), list) else rule_coarse.asset_types
    risk_tags = data.get("risk_tags") if isinstance(data.get("risk_tags"), list) else rule_coarse.risk_tags
    reasons = data.get("reasons") if isinstance(data.get("reasons"), list) else rule_coarse.reasons
    score = int(data.get("score") or rule_screen.total_score)
    pool = data.get("pool") or rule_screen.decision_pool
    if pool not in {"scan", "initial", "trackable", "review", "archive"}:
        pool = rule_screen.decision_pool
    coarse = CoarseFilterResult(
        passed=bool(asset_types) or rule_coarse.passed,
        reasons=[str(x) for x in reasons] or rule_coarse.reasons,
        asset_types=[str(x) for x in asset_types],
        risk_tags=[str(x) for x in risk_tags],
        model_name=data.get("model_name", ""),
    )
    screening = ScreeningScore(
        total_score=score,
        decision_pool=pool,
        factors={**rule_screen.factors, "LLM综合判断": score},
        reasons=[str(x) for x in reasons] or rule_screen.reasons,
        model_name=data.get("model_name", ""),
    )
    return coarse, screening, {"llm_used": True, **data}
