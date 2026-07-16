from __future__ import annotations

import re

from .models import AssetRecord


def _record_text(record: AssetRecord) -> str:
    section_text = " ".join(
        f"{section.get('title', '')} {section.get('content', '')}"
        for section in (record.detail_sections or [])
        if isinstance(section, dict)
    )
    return " ".join(
        x
        for x in [
            record.title,
            record.source_platform,
            record.city,
            record.district,
            record.amount_text,
            record.extracted_summary,
            record.collateral_detail,
            record.detail_text,
            record.raw_text,
            section_text,
        ]
        if x
    )


def _add_tag(tags: list[dict[str, str]], seen: set[str], label: str, tone: str) -> None:
    if label in seen:
        return
    seen.add(label)
    tags.append({"label": label, "tone": tone})


def _has(text: str, pattern: str) -> bool:
    return re.search(pattern, text) is not None


def generate_asset_tags(record: AssetRecord, max_tags: int = 6) -> list[dict[str, str]]:
    """Generate stable user-facing tags for asset pool tables."""
    text = _record_text(record)
    tags: list[dict[str, str]] = []
    seen: set[str] = set()

    has_security = _has(text, r"抵押|抵质押|质押|担保物权")
    if _has(text, r"土地|国有土地|工业用地|建设用地|土地使用权|土地证"):
        _add_tag(tags, seen, "土地抵押" if has_security else "土地权益", "green")
    if _has(text, r"房产|房地产|不动产|建筑物|厂房|商铺|写字楼|办公楼|住宅|公寓|酒店|商业用房|车库|仓库"):
        _add_tag(tags, seen, "房产抵押" if has_security else "房产", "green")
    if _has(text, r"在建工程|工程项目|开发项目|施工|竣工"):
        _add_tag(tags, seen, "在建工程", "green")
    if _has(text, r"担保|保证|连带责任|保证人|抵押担保|质押担保"):
        _add_tag(tags, seen, "担保", "amber")
    if _has(text, r"股权|股票|股份|基金份额|合伙份额|出资份额"):
        _add_tag(tags, seen, "股权/份额", "blue")
    if _has(text, r"债权|本金|利息|不良债权|金融债权|应收账款"):
        _add_tag(tags, seen, "债权", "blue")
    if _has(text, r"资产包|多户|多项资产|批量|户债权|户资产"):
        _add_tag(tags, seen, "资产包", "amber")
    if _has(text, r"招商|招募|推介"):
        _add_tag(tags, seen, "招商", "amber")
    if _has(text, r"拍卖|竞价|挂牌|司法拍卖|变卖|降价"):
        _add_tag(tags, seen, "竞价", "amber")
    if _has(text, r"查封|冻结|破产|清算|诉讼|执行|瑕疵|占用|租赁|风险"):
        _add_tag(tags, seen, "风险提示", "red")

    if not tags:
        _add_tag(tags, seen, "待识别", "gray")
    if len(tags) > max_tags:
        return tags[:max_tags]
    return tags


def apply_asset_tags(record: AssetRecord) -> AssetRecord:
    record.asset_tags = generate_asset_tags(record)
    return record
