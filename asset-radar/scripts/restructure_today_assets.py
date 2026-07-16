from __future__ import annotations

import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.asset_radar.crawler import enrich_notice_detail
from src.asset_radar.llm_analysis import structure_notice_detail_with_llm
from src.asset_radar.models import RawNotice
from src.asset_radar.storage import JsonAssetStore


STORE_PATH = ROOT / "data" / "asset_pool.local.json"


def _source_id_for(record) -> str:
    source_text = f"{record.source_platform} {record.source_url}"
    if "东方资产" in source_text or "sales.coamc.com.cn" in source_text:
        return "coamc_orient_disposal"
    if "长城国际" in source_text or "gwamcc.com.hk" in source_text:
        return "gwamcc_international_disposal"
    if "信达" in source_text:
        return "cinda_amc_asset_disposal"
    if "中信" in source_text:
        return "citic_amc_asset_disposal"
    return ""


def _make_notice(record) -> RawNotice:
    source_id = _source_id_for(record)
    return RawNotice(
        source_platform=record.source_platform,
        source_url=record.source_url,
        title=record.title,
        notice_date=record.latest_notice_date,
        raw_text=record.raw_text or record.detail_text or record.title,
        debtor=record.debtor,
        disposal_agency=record.disposal_agency,
        city=record.city,
        district=record.district,
        amount_text=record.amount_text,
        detail_text=record.detail_text,
        attachments=record.attachment_summaries,
        metadata={"source_id": source_id},
    )


def _section_content(sections: list[dict[str, str]], title_keyword: str) -> str:
    for section in sections:
        title = str(section.get("title") or "")
        if title_keyword in title:
            return str(section.get("content") or "")
    return ""


def restructure_record(record):
    notice = _make_notice(record)
    if notice.source_url:
        should_refresh_detail = (
            not notice.detail_text
            or notice.metadata.get("source_id") in {"coamc_orient_disposal", "gwamcc_international_disposal"}
        )
        if should_refresh_detail:
            notice.detail_text = ""
            notice = enrich_notice_detail(notice, include_attachments=False, detail_limit=16000)

    sections = structure_notice_detail_with_llm(
        notice,
        {
            "summary": record.extracted_summary,
            "collateral_detail": record.collateral_detail,
            "amount_text": record.amount_text,
        },
    )
    record.detail_sections = sections
    record.detail_text = notice.detail_text or record.detail_text
    record.raw_text = notice.raw_text or record.raw_text
    summary = _section_content(sections, "概览")
    collateral = _section_content(sections, "标的") or _section_content(sections, "抵质押")
    if summary:
        record.extracted_summary = summary[:900]
    if collateral:
        record.collateral_detail = collateral[:900]
    record.node_models["detail_readability_node"] = "deepseek/deepseek-v4-pro"
    record.notes.append(f"{datetime.utcnow().isoformat()} batch_detail_restructured")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="Restructure today's Asset Radar records with the readability LLM.")
    parser.add_argument("--date", default=datetime.now().date().isoformat())
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    store = JsonAssetStore(STORE_PATH)
    records = store.load()
    selected = [
        record for record in records.values()
        if record.first_seen_at.date().isoformat() == args.date
    ]
    selected.sort(key=lambda record: (record.source_platform, record.title))
    if args.limit > 0:
        selected = selected[:args.limit]

    if not args.no_backup and STORE_PATH.exists():
        backup = STORE_PATH.with_name(
            f"{STORE_PATH.stem}.before_restructure_{datetime.now().strftime('%Y%m%d_%H%M%S')}{STORE_PATH.suffix}"
        )
        shutil.copy2(STORE_PATH, backup)
        print(f"backup={backup}")

    total = len(selected)
    print(f"selected={total} date={args.date}")
    workers = max(1, args.workers)
    if workers == 1:
        for index, record in enumerate(selected, start=1):
            print(f"[{index}/{total}] {record.asset_id} {record.title[:80]}", flush=True)
            records[record.asset_id] = restructure_record(record)
            store.save(records)
    else:
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(restructure_record, record): record for record in selected}
            for future in as_completed(future_map):
                original = future_map[future]
                completed += 1
                try:
                    record = future.result()
                    records[record.asset_id] = record
                    status = "ok"
                except Exception as exc:
                    status = f"error={type(exc).__name__}:{exc}"
                store.save(records)
                print(f"[{completed}/{total}] {status} {original.asset_id} {original.title[:80]}", flush=True)

    print("done")


if __name__ == "__main__":
    main()
