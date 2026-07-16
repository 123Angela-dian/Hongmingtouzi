from __future__ import annotations

import json
import re

from .llm_client import ChatMessage, CherryINClient, CherryINConfigError, extract_text
from .model_config import model_for_node
from .models import AssetRecord, CoarseFilterResult, RawNotice, ScreeningScore
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


SOURCE_CATEGORY_RULES = {
    "抵债物资产处置公告": True,
    "债权资产处置公告": True,
    "资产包处置公告": True,
    "股权资产处置公告": True,
    "多项资产处置公告": True,
    "资产包招商公告": True,
    "推介信息": False,
}


_SOURCE_CATEGORY_CACHE: dict[str, dict] = {}


def classify_source_category_with_glm(category_name: str, sample_titles: list[str] | None = None) -> dict:
    """Use GLM once per dropdown/category to decide whether a source bucket is relevant."""
    sample_titles = sample_titles or []
    cache_key = "\n".join([category_name, *sample_titles[:5]])
    if cache_key in _SOURCE_CATEGORY_CACHE:
        return _SOURCE_CATEGORY_CACHE[cache_key]

    fallback_include = SOURCE_CATEGORY_RULES.get(category_name)
    prompt = """
你是资产雷达的数据源分类助手。只返回 JSON，不要 Markdown。
判断一个公告下拉分类是否应该进入资产雷达扫描范围。
纳入范围：债权资产、抵债物中的土地/房地产/在建工程/建筑物等不动产、上市或非上市公司股权、资产包、多项资产组合、资产包招商。
剔除范围：车辆、珠宝、消费品、普通新闻/营销推介、无法形成可追踪资产线索的栏目。
格式：{"include": true/false, "scope": "简短归类", "reason": "一句话原因"}
"""
    user_text = json.dumps({"category_name": category_name, "sample_titles": sample_titles[:8]}, ensure_ascii=False)
    try:
        profile = model_for_node("source_taxonomy_node")
        client = CherryINClient()
        response = client.chat(
            profile,
            [ChatMessage(role="system", content=prompt), ChatMessage(role="user", content=user_text)],
            max_tokens=400,
            temperature=0,
            timeout=30,
        )
        data = _safe_json(extract_text(response))
        result = {
            "include": bool(data.get("include")),
            "scope": str(data.get("scope") or ""),
            "reason": str(data.get("reason") or ""),
            "model_name": profile.model,
            "llm_used": True,
        }
    except (CherryINConfigError, Exception) as exc:
        result = {
            "include": bool(fallback_include),
            "scope": "rule_fallback",
            "reason": f"GLM unavailable; fallback rule used: {exc}",
            "model_name": model_for_node("source_taxonomy_node").model,
            "llm_used": False,
        }

    if fallback_include is not None:
        result["include"] = bool(fallback_include)
    _SOURCE_CATEGORY_CACHE[cache_key] = result
    return result


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
    text = re.sub(r"\[[^\]]{0,40}\]\s*-->", " ", (notice.detail_text or notice.raw_text or notice.title).strip())
    text = re.sub(r"\s+", " ", text)
    summary = str(analysis.get("summary") or notice.metadata.get("source_scan_summary") or notice.title)
    collateral = str(analysis.get("collateral_detail") or "")
    amount = str(analysis.get("amount_text") or notice.amount_text or "")
    risk_tags = analysis.get("risk_tags") if isinstance(analysis.get("risk_tags"), list) else []
    asset_types = analysis.get("asset_types") if isinstance(analysis.get("asset_types"), list) else []

    def pick_sentences(terms: tuple[str, ...], limit: int = 520) -> str:
        pieces = re.split(r"(?<=[。；;])\s*|\s{2,}|(?=标的资产清单|租赁期间|联系人|特别提示|风险提示|公告期)", text)
        hits = []
        for piece in pieces:
            piece = piece.strip()
            if len(piece) < 8:
                continue
            if any(term in piece for term in terms):
                if len(piece) > 220:
                    positions = [piece.find(term) for term in terms if term in piece]
                    start = max(0, min(positions) - 70) if positions else 0
                    piece = piece[start:start + 220]
                hits.append(piece)
            if len(" ".join(hits)) >= limit:
                break
        return " ".join(hits)[:limit]

    inferred_collateral = pick_sentences((
        "抵押物", "质押物", "标的资产", "资产清单", "不动产", "房地产", "房产", "土地", "建筑面积", "股权", "债权名称",
    ))
    inferred_amount = pick_sentences((
        "本金", "利息", "债权", "金额", "万元", "元", "租金", "保证金", "起拍", "评估价",
    ), limit=360)
    inferred_disposal = pick_sentences((
        "处置", "招商", "转让", "拍卖", "竞价", "招租", "公告期", "报名", "联系人", "联系方式",
    ), limit=420)
    inferred_risk = pick_sentences((
        "风险", "瑕疵", "自行调查", "不作保证", "可能", "破产", "查封", "抵债", "现状",
    ), limit=420)
    collateral_quality_terms = ("抵押", "质押", "标的资产", "不动产", "房地产", "房产", "土地", "建筑面积", "股权", "债权")
    usable_collateral = collateral if any(term in collateral for term in collateral_quality_terms) else ""

    sections = [
        {"title": "项目概览", "content": summary[:500]},
        {"title": "标的/抵质押物", "content": inferred_collateral or usable_collateral[:700] or "公告未直接披露清晰的抵质押物或标的资产，需要继续查看原文和附件。"},
        {"title": "金额与处置方式", "content": "；".join(x for x in [amount[:300], inferred_amount, inferred_disposal] if x)[:760] or "公告未提取到明确金额；后续可从附件、拍卖页或补充公告继续核验。"},
        {"title": "资产类型", "content": "、".join(str(x) for x in asset_types) or "待进一步识别"},
        {"title": "风险提示", "content": "、".join(str(x) for x in risk_tags) or inferred_risk or "暂无明确风险标签，仍需关注查封、租赁、欠费、权属瑕疵等信息。"},
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


NOISE_PATTERNS = (
    "The handshake operation timed out",
    "返回中国东方营销网站首页",
    "Document ",
    "<html",
    "</html",
    "<script",
    "function(",
    "404 Not Found",
    "403 Forbidden",
    "AccessDenied",
    "Request Error",
    "Bad Gateway",
    "Gateway Timeout",
    "�",
)


def _asset_readability_payload(record: AssetRecord) -> dict:
    return {
        "title": record.title,
        "source": record.source_platform,
        "city": record.city,
        "amount_text": record.amount_text,
        "summary": record.extracted_summary,
        "collateral_detail": record.collateral_detail,
        "detail_sections": record.detail_sections,
        "detail_text_sample": (record.detail_text or record.raw_text or "")[:1800],
    }


def _deterministic_readability_review(record: AssetRecord) -> dict:
    text = json.dumps(_asset_readability_payload(record), ensure_ascii=False)
    section_text = " ".join(
        f"{section.get('title', '')} {section.get('content', '')}"
        for section in (record.detail_sections or [])
        if isinstance(section, dict)
    )
    reasons: list[str] = []
    if not record.detail_sections:
        reasons.append("缺少结构化详情分段")
    if not section_text.strip():
        reasons.append("结构化详情为空")
    if len(section_text.strip()) < 80:
        reasons.append("结构化详情过短，无法支撑用户阅读")
    for pattern in NOISE_PATTERNS:
        if pattern in text:
            reasons.append(f"发现网页噪音或错误文本：{pattern}")
            break
    if text.count("{") + text.count("}") > 30 or text.count("<div") > 3:
        reasons.append("疑似混入网页源码或 JSON 噪音")
    return {
        "passed": not reasons,
        "reasons": reasons or ["规则检查通过"],
        "cleanliness_score": 90 if not reasons else 35,
        "model_name": "deterministic_readability_rules",
        "llm_used": False,
    }


def review_asset_readability_with_glm(record: AssetRecord) -> dict:
    """Review whether a newly pooled asset is readable and clean enough for users."""
    rule_review = _deterministic_readability_review(record)
    if not rule_review["passed"]:
        return rule_review

    profile = model_for_node("asset_readability_reviewer_node")
    prompt = """
你是资产雷达的入池质量 reviewer。请只返回 JSON，不要 Markdown。
你要检查一个即将进入资产池的资产信息是否“用户可读且干净”。

通过标准：
1. detail_sections 有清楚标题和正文，用户能理解资产是什么、标的/抵质押物或权益是什么、处置方式/金额/风险在哪里。
2. 不包含网页导航、乱码、HTML/JS、接口错误、超时提示、重复无意义文本。
3. 信息可以有“未披露/待核验”，但不能整段都是抓取失败或网页噪音。

返回格式：
{
  "passed": true/false,
  "cleanliness_score": 0-100,
  "reasons": ["不超过3条原因"],
  "retry_focus": "如果不通过，说明应该重新抓取正文、附件、还是重新整理"
}
"""
    user_text = json.dumps(_asset_readability_payload(record), ensure_ascii=False)
    try:
        client = CherryINClient()
        response = client.chat(
            profile,
            [ChatMessage(role="system", content=prompt), ChatMessage(role="user", content=user_text[:6000])],
            max_tokens=500,
            temperature=0,
            timeout=45,
        )
        data = _safe_json(extract_text(response))
        return {
            "passed": bool(data.get("passed")),
            "cleanliness_score": int(data.get("cleanliness_score") or 0),
            "reasons": [str(x) for x in (data.get("reasons") or [])][:3],
            "retry_focus": str(data.get("retry_focus") or ""),
            "model_name": profile.model,
            "llm_used": True,
        }
    except (CherryINConfigError, Exception) as exc:
        return {
            **rule_review,
            "reasons": [*rule_review["reasons"], f"GLM reviewer unavailable: {exc}"][:3],
            "model_name": profile.model,
        }


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
