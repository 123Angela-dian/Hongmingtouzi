from __future__ import annotations

import json
from pathlib import Path

from .models import AssetRecord


class JsonAssetStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, AssetRecord]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        return {item["asset_id"]: AssetRecord.model_validate(item) for item in data}

    def save(self, records: dict[str, AssetRecord]) -> None:
        payload = [
            record.model_dump(mode="json") for record in sorted(records.values(), key=lambda x: x.asset_id)
        ]
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
