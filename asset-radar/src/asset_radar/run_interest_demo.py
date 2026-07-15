from __future__ import annotations

import json
from pathlib import Path

from .models import AssetRecord, CoarseFilterResult, RawNotice, ScreeningScore
from .rules import stable_asset_id
from .storage import JsonAssetStore
from .tracing import flush_traces
from .workflow import run_asset_radar_workflow


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    store_path = repo_root / "data" / "interest_demo_pool.local.json"
    store = JsonAssetStore(store_path)

    base_notice = RawNotice(
        source_platform="interest_demo",
        source_url="https://example.com/auction/asset-001",
        title="上海核心商业房产抵押债权首次挂牌",
        raw_text="债务人上海样本置业有限公司，抵押物为上海市核心区商业房产，抵押、质押、担保齐备，首次挂牌处置。",
        debtor="上海样本置业有限公司",
        city="上海",
        amount_text="不设金额限制",
    )
    asset_id = stable_asset_id(base_notice)
    interested_record = AssetRecord(
        asset_id=asset_id,
        title=base_notice.title,
        debtor=base_notice.debtor,
        city=base_notice.city,
        source_platform=base_notice.source_platform,
        source_url=base_notice.source_url,
        raw_text=base_notice.raw_text,
        pool="trackable",
        interested=True,
        coarse_filter=CoarseFilterResult(passed=True, reasons=["seed interested asset"]),
        screening_score=ScreeningScore(total_score=85, decision_pool="trackable", factors={}, reasons=[]),
    )
    store.save({asset_id: interested_record})

    update_notice = RawNotice(
        source_platform="interest_demo",
        source_url="https://example.com/auction/asset-001-round-2",
        title="上海核心商业房产抵押债权二拍降价重新挂牌",
        raw_text="同一资产二拍，降价重新挂牌，可能拆分出售部分商业面积。",
        debtor="上海样本置业有限公司",
        city="上海",
        amount_text="不设金额限制",
    )
    result = run_asset_radar_workflow([update_notice], store_path=store_path, dry_run=False)
    print(json.dumps({
        "alerts": result.alerts,
        "records": [
            {
                "asset_id": record.asset_id,
                "title": record.title,
                "announcement_count": record.announcement_count,
                "auction_count": record.auction_count,
                "is_price_drop": record.is_price_drop,
                "is_split_sale": record.is_split_sale,
                "is_relisted": record.is_relisted,
                "node_models": record.node_models,
            }
            for record in result.records
        ],
        "model_plan": {
            key: value["model"] for key, value in result.model_plan.items()
        },
    }, ensure_ascii=False, indent=2))
    flush_traces()


if __name__ == "__main__":
    main()
