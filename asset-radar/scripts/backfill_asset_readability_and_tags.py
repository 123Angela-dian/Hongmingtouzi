from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.asset_radar.asset_tags import apply_asset_tags
from src.asset_radar.models import AssetRecord, RawNotice
from src.asset_radar.storage import JsonAssetStore
from src.asset_radar.workflow import asset_readability_quality_gate


STORE_PATH = ROOT / "data" / "asset_pool.local.json"
PROGRESS_PATH = ROOT / "data" / "backfill_readability_tags.progress.json"
DONE_NOTE = "backfilled readability reviewer and asset tags"


def record_to_notice(record: AssetRecord) -> RawNotice:
    source_id = "coamc_orient_disposal" if "sales.coamc.com.cn" in record.source_url else ""
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


def progress(stage: str, message: str, data: dict | None = None) -> None:
    data = data or {}
    asset_id = data.get("asset_id", "")
    attempt = data.get("attempt", "")
    suffix = f" asset_id={asset_id}" if asset_id else ""
    suffix += f" attempt={attempt}" if attempt else ""
    print(f"[{stage}] {message}{suffix}", flush=True)


def is_done(record: AssetRecord) -> bool:
    return bool(record.asset_tags) and any(DONE_NOTE in note for note in record.notes)


def write_progress(status: str, done: int, total: int, current: str = "") -> None:
    payload = {
        "status": status,
        "done": done,
        "total": total,
        "percent": round((done / total * 100) if total else 100, 1),
        "current": current,
        "updated_at": datetime.utcnow().isoformat(),
    }
    PROGRESS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def print_progress_bar(done: int, total: int) -> None:
    width = 28
    percent = (done / total) if total else 1
    filled = int(width * percent)
    bar = "#" * filled + "-" * (width - filled)
    print(f"\rProgress [{bar}] {done}/{total} {percent * 100:5.1f}%", end="", flush=True)
    if done >= total:
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill existing asset readability sections, reviewer results, and tags.")
    parser.add_argument("--limit", type=int, default=0, help="Optional max records to process.")
    parser.add_argument("--start", type=int, default=0, help="Optional zero-based start offset.")
    parser.add_argument("--include-done", action="store_true", help="Reprocess records that already have backfill notes and tags.")
    parser.add_argument("--no-backup", action="store_true", help="Skip creating a backup before this batch.")
    args = parser.parse_args()

    store = JsonAssetStore(STORE_PATH)
    records = store.load()
    if not records:
        print("No records found.")
        return

    if not args.no_backup:
        backup = STORE_PATH.with_name(
            f"{STORE_PATH.stem}.before_readability_tags_{datetime.now().strftime('%Y%m%d_%H%M%S')}{STORE_PATH.suffix}"
        )
        shutil.copy2(STORE_PATH, backup)
        print(f"Backup created: {backup}")

    ordered = sorted(records.values(), key=lambda item: (item.first_seen_at, item.asset_id))
    selected = ordered[args.start:]
    if not args.include_done:
        selected = [record for record in selected if not is_done(record)]
    if args.limit:
        selected = selected[: args.limit]

    total = len(selected)
    write_progress("running", 0, total)
    for index, record in enumerate(selected, start=1):
        write_progress("running", index - 1, total, record.title)
        print_progress_bar(index - 1, total)
        print(f"\n[{index}/{total}] {record.title} ({record.pool})", flush=True)
        notice = record_to_notice(record)
        updated = asset_readability_quality_gate(record, notice, progress=progress)
        apply_asset_tags(updated)
        updated.notes.append(f"{datetime.utcnow().isoformat()} {DONE_NOTE}")
        records[updated.asset_id] = updated
        store.save(records)
        tag_labels = "、".join(tag.get("label", "") for tag in updated.asset_tags)
        print(f"Saved: pool={updated.pool} tags={tag_labels}", flush=True)
        write_progress("running", index, total, record.title)
        print_progress_bar(index, total)

    write_progress("done", total, total)
    print(f"\nDone. Processed {total} records.")


if __name__ == "__main__":
    main()
