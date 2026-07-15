from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from .crawler import enrich_notice_detail
from .model_config import dedupe_model, model_for_node, model_plan
from .models import AssetRecord, AuctionEvent, DedupeResult, RawNotice, WorkflowResult
from .llm_analysis import llm_or_rule_analysis, structure_notice_detail_with_llm
from .rules import coarse_filter, detailed_screening, detect_update_events, extract_city_from_text, stable_asset_id
from .storage import JsonAssetStore
from .tracing import traceable_node


ProgressCallback = Callable[[str, str, dict | None], None]
StopCallback = Callable[[], bool]
AMC_EXPAND_TERMS = ("资产处置", "债权", "债务", "催收", "不良", "转让", "竞价", "招商", "资产包", "合伙份额")


@traceable_node("source_scan_node")
def source_scan_node(notices: list[RawNotice]) -> list[RawNotice]:
    for notice in notices:
        notice.metadata["source_scan_model"] = "basic_parser"
    return notices


@traceable_node("coarse_filter_node")
def coarse_filter_node(notice: RawNotice):
    return coarse_filter(notice)


@traceable_node("dedupe_node")
def dedupe_node(notice: RawNotice, records: dict[str, AssetRecord]) -> DedupeResult:
    asset_id = stable_asset_id(notice)
    normalized_debtor = notice.debtor.strip().lower()
    if normalized_debtor:
        for record in records.values():
            if record.debtor.strip().lower() == normalized_debtor and record.city == notice.city:
                events = detect_update_events(record.raw_text, notice.raw_text)
                profile = dedupe_model(complex_case=False)
                return DedupeResult(
                    decision="existing_asset_update",
                    asset_id=record.asset_id,
                    matched_asset_id=record.asset_id,
                    reasons=["debtor and city matched existing asset"],
                    update_events=events,
                    complexity="simple",
                    model_name=profile.model,
                )
    if notice.source_url:
        for record in records.values():
            if record.source_url and record.source_url == notice.source_url:
                events = detect_update_events(record.raw_text, notice.raw_text)
                profile = dedupe_model(complex_case=False)
                return DedupeResult(
                    decision="existing_asset_update",
                    asset_id=record.asset_id,
                    matched_asset_id=record.asset_id,
                    reasons=["source_url matched existing asset"],
                    update_events=events,
                    complexity="simple",
                    model_name=profile.model,
                )
    if asset_id in records:
        events = detect_update_events(records[asset_id].raw_text, notice.raw_text)
        profile = dedupe_model(complex_case=False)
        return DedupeResult(
            decision="existing_asset_update",
            asset_id=asset_id,
            matched_asset_id=asset_id,
            reasons=["stable identity matched existing asset"],
            update_events=events,
            complexity="simple",
            model_name=profile.model,
        )
    has_near_match_context = bool(records) and bool(normalized_debtor or notice.city)
    profile = dedupe_model(complex_case=has_near_match_context)
    return DedupeResult(
        decision="new_asset",
        asset_id=asset_id,
        reasons=["no duplicate found"],
        complexity="complex" if has_near_match_context else "simple",
        model_name=profile.model,
    )


@traceable_node("detailed_screening_node")
def detailed_screening_node(notice: RawNotice):
    return detailed_screening(notice)


def should_expand_before_coarse_filter(notice: RawNotice) -> bool:
    source_type = str(notice.metadata.get("source_type") or "")
    if source_type != "amc_notice":
        return False
    text = f"{notice.title} {notice.amount_text} {notice.disposal_agency}"
    return any(term in text for term in AMC_EXPAND_TERMS)


@traceable_node("asset_pool_update_node")
def asset_pool_update_node(
    notice: RawNotice,
    records: dict[str, AssetRecord],
    dedupe: DedupeResult,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
) -> AssetRecord:
    if dedupe.matched_asset_id and dedupe.matched_asset_id in records:
        record = records[dedupe.matched_asset_id]
        if not notice.city:
            notice.city = extract_city_from_text(notice.title, notice.raw_text, notice.detail_text)
        if record.pool == "archive":
            record.pool = "initial"
        record.announcement_count += 1
        record.latest_notice_date = notice.notice_date or record.latest_notice_date
        record.raw_text = notice.raw_text or record.raw_text
        record.detail_text = notice.detail_text or record.detail_text
        if notice.city and not record.city:
            record.city = notice.city
        if record.detail_text and (notice.detail_text or not record.detail_sections):
            record.detail_sections = structure_notice_detail_with_llm(notice)
        record.attachment_summaries = notice.attachments or record.attachment_summaries
        record.notes.append(f"{datetime.utcnow().isoformat()} update from {notice.source_platform}")
        record.node_models["asset_pool_update_node"] = model_for_node("asset_pool_update_node").model
        record.node_models["dedupe_node"] = dedupe.model_name
        if dedupe.update_events:
            event = AuctionEvent(
                event_type=";".join(dedupe.update_events),
                event_time=notice.notice_date,
                source_url=notice.source_url,
                description="Existing asset updated by new notice",
            )
            record.auction_history.append(event)
            if "new_auction_round" in dedupe.update_events:
                record.auction_count += 1
                record.latest_auction_time = notice.notice_date or record.latest_auction_time
            if "price_drop_or_later_round" in dedupe.update_events:
                record.is_price_drop = True
            if "split_sale" in dedupe.update_events:
                record.is_split_sale = True
            if "relisted" in dedupe.update_events:
                record.is_relisted = True
        records[record.asset_id] = record
        return record

    if not notice.city:
        notice.city = extract_city_from_text(notice.title, notice.raw_text, notice.detail_text)
    basic_coarse = coarse_filter_node(notice)
    basic_screening = detailed_screening_node(notice)
    record = AssetRecord(
        asset_id=dedupe.asset_id,
        title=notice.title,
        debtor=notice.debtor,
        city=notice.city,
        district=notice.district,
        source_platform=notice.source_platform,
        source_url=notice.source_url,
        disposal_agency=notice.disposal_agency,
        amount_text=notice.amount_text,
        raw_text=notice.raw_text,
        detail_text=notice.detail_text,
        extracted_summary=str(notice.metadata.get("source_scan_summary") or ""),
        collateral_detail=(notice.raw_text or notice.title)[:300],
        attachment_summaries=notice.attachments,
        pool="scan",
        coarse_filter=basic_coarse,
        screening_score=basic_screening,
        latest_notice_date=notice.notice_date,
        notes=[f"created by basic scan from {notice.source_platform}"],
        node_models={
            "source_scan_node": notice.metadata.get("source_scan_model", "basic_parser"),
            "coarse_filter_node": basic_coarse.model_name,
            "dedupe_node": dedupe.model_name,
            "asset_pool_update_node": model_for_node("asset_pool_update_node").model,
        },
    )
    records[record.asset_id] = record

    if not basic_coarse.passed:
        if should_expand_before_coarse_filter(notice):
            if progress:
                progress(
                    "expand",
                    f"标题信息不足，展开详情搜索：{notice.title}",
                    {"asset_id": record.asset_id, "source": notice.source_platform},
                )
            notice = enrich_notice_detail(
                notice,
                progress=progress,
                include_attachments=False,
                detail_limit=6000,
            )
            expanded_coarse = coarse_filter_node(notice)
            expanded_screening = detailed_screening_node(notice)
            if not notice.city:
                notice.city = extract_city_from_text(notice.title, notice.raw_text, notice.detail_text)
            record.city = notice.city
            record.raw_text = notice.raw_text
            record.detail_text = notice.detail_text
            record.collateral_detail = (notice.detail_text or notice.raw_text or notice.title)[:300]
            record.coarse_filter = expanded_coarse
            record.screening_score = expanded_screening
            record.notes.append(f"{datetime.utcnow().isoformat()} lightweight detail expansion before initial filter")
            records[record.asset_id] = record
            if expanded_coarse.passed:
                basic_coarse = expanded_coarse
                basic_screening = expanded_screening
            else:
                if progress:
                    progress("scan_pool", f"展开后仍留在扫描池：{notice.title}", {"pool": "scan", "asset_id": record.asset_id})
                return record
        else:
            if progress:
                progress("scan_pool", f"进入扫描池：{notice.title}", {"pool": "scan", "asset_id": record.asset_id})
            return record

    if basic_coarse.passed and progress:
        progress("initial_candidate", f"通过轻量初筛：{notice.title}", {"asset_id": record.asset_id})

    if basic_coarse.passed and not notice.detail_text:
        notice = enrich_notice_detail(notice, progress=progress, include_attachments=False, detail_limit=12000)

    if not basic_coarse.passed:
        if progress:
            progress("scan_pool", f"进入扫描池：{notice.title}", {"pool": "scan", "asset_id": record.asset_id})
        return record

    if progress:
        progress("screening", f"初筛命中，开始归一化：{notice.title}", {"asset_id": record.asset_id})
    notice = enrich_notice_detail(notice, progress=progress)
    if not notice.city:
        notice.city = extract_city_from_text(notice.title, notice.raw_text, notice.detail_text)
    coarse, screening, analysis = llm_or_rule_analysis(notice)
    if analysis.get("debtor") and not notice.debtor:
        notice.debtor = str(analysis.get("debtor"))
    if analysis.get("city") and not notice.city:
        notice.city = str(analysis.get("city"))
    if analysis.get("amount_text") and not notice.amount_text:
        notice.amount_text = str(analysis.get("amount_text"))
    record.title = notice.title
    record.debtor = notice.debtor
    record.city = notice.city
    record.district = notice.district
    record.amount_text = notice.amount_text
    record.raw_text = notice.raw_text
    record.detail_text = notice.detail_text
    record.detail_sections = structure_notice_detail_with_llm(notice, analysis)
    record.extracted_summary = str(analysis.get("summary") or "")
    record.collateral_detail = str(analysis.get("collateral_detail") or (notice.detail_text or notice.raw_text)[:300])
    record.attachment_summaries = notice.attachments
    record.coarse_filter = coarse
    record.screening_score = screening
    record.notes.append(f"{datetime.utcnow().isoformat()} normalized after initial screening")
    record.node_models.update({
        "coarse_filter_node": coarse.model_name,
        "detailed_screening_node": screening.model_name,
        "detail_readability_node": model_for_node("detail_readability_node").model,
    })
    if screening.decision_pool == "trackable":
        record.pool = "trackable"
    elif screening.decision_pool == "review":
        record.pool = "review"
    elif screening.decision_pool == "initial":
        record.pool = "initial"
    elif coarse.passed:
        record.pool = "initial"
    records[record.asset_id] = record
    if progress:
        progress("pool_update", f"入池完成：{notice.title} -> {record.pool}", {"pool": record.pool, "asset_id": record.asset_id})
    return record


@traceable_node("interest_alert_node")
def interest_alert_node(records: list[AssetRecord]) -> list[str]:
    alert_model = model_for_node("interest_alert_node").model
    alerts: list[str] = []
    for record in records:
        if not record.interested:
            continue
        changes = []
        if record.is_price_drop:
            changes.append("降价/后续轮次")
        if record.is_split_sale:
            changes.append("拆分出售")
        if record.is_relisted:
            changes.append("重新挂牌")
        if record.latest_auction_time:
            changes.append(f"最新拍卖时间 {record.latest_auction_time}")
        if changes:
            alerts.append(f"{record.title}: {'、'.join(changes)} [model={alert_model}]")
    return alerts


@traceable_node("asset_radar_workflow")
def run_asset_radar_workflow(
    notices: list[RawNotice],
    store_path: Path,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
    should_stop: StopCallback | None = None,
    save_on_stop: StopCallback | None = None,
) -> WorkflowResult:
    store = JsonAssetStore(store_path)
    records = store.load()
    processed: list[AssetRecord] = []

    raw_notices = source_scan_node(notices)
    if progress:
        progress("workflow", f"开始工作流处理 {len(raw_notices)} 条扫描线索", {"count": len(raw_notices)})
    for notice in raw_notices:
        if should_stop and should_stop():
            if progress:
                progress("stop", "已收到停止请求，结束工作流处理", {"processed": len(processed)})
            break
        if progress:
            progress("dedupe", f"查重：{notice.title}", {"source": notice.source_platform})
        dedupe = dedupe_node(notice, records)
        record = asset_pool_update_node(notice, records, dedupe, dry_run=dry_run, progress=progress)
        processed.append(record)

    stopped = bool(should_stop and should_stop())
    should_save = not stopped or bool(save_on_stop and save_on_stop())
    if not dry_run and should_save:
        store.save(records)

    all_records = list(records.values())
    return WorkflowResult(
        raw_count=len(raw_notices),
        scan_count=sum(1 for r in all_records if r.pool == "scan"),
        initial_count=sum(1 for r in all_records if r.pool == "initial"),
        trackable_count=sum(1 for r in all_records if r.pool == "trackable"),
        review_count=sum(1 for r in all_records if r.pool == "review"),
        archive_count=sum(1 for r in all_records if r.pool == "archive"),
        alerts=interest_alert_node(all_records),
        records=processed,
        model_plan=model_plan(),
    )
