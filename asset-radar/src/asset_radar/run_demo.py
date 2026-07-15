from __future__ import annotations

import json
from pathlib import Path

from .excel_loader import load_marked_notices
from .tracing import flush_traces
from .workflow import run_asset_radar_workflow


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source_dir = Path(r"C:\Users\HP\Documents\xwechat_files\adsfd121_36f2\msg\file\2026-07")
    store_path = repo_root / "data" / "asset_pool.local.json"

    notices = load_marked_notices(source_dir, limit=12)
    result = run_asset_radar_workflow(notices, store_path=store_path, dry_run=True)
    summary = {
        "raw_count": result.raw_count,
        "initial_count": result.initial_count,
        "trackable_count": result.trackable_count,
        "review_count": result.review_count,
        "archive_count": result.archive_count,
        "alerts": result.alerts,
        "model_plan": {
            key: value["model"] for key, value in result.model_plan.items()
        },
        "processed": [
            {
                "asset_id": record.asset_id,
                "title": record.title,
                "pool": record.pool,
                "score": record.screening_score.total_score if record.screening_score else None,
                "factors": record.screening_score.factors if record.screening_score else {},
                "node_models": record.node_models,
            }
            for record in result.records[:10]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    flush_traces()


if __name__ == "__main__":
    main()
