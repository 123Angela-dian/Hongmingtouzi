from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


PoolName = Literal["scan", "initial", "trackable", "review", "archive"]


class RawNotice(BaseModel):
    source_platform: str
    source_url: str = ""
    title: str
    notice_date: date | None = None
    raw_text: str
    debtor: str = ""
    disposal_agency: str = ""
    city: str = ""
    district: str = ""
    amount_text: str = ""
    detail_text: str = ""
    attachments: list[dict[str, str]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CoarseFilterResult(BaseModel):
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    asset_types: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    model_name: str = ""


class DedupeResult(BaseModel):
    decision: Literal["new_asset", "existing_asset_update", "possible_duplicate"]
    asset_id: str
    matched_asset_id: str | None = None
    reasons: list[str] = Field(default_factory=list)
    update_events: list[str] = Field(default_factory=list)
    complexity: Literal["simple", "complex"] = "simple"
    model_name: str = ""


class ScreeningScore(BaseModel):
    total_score: int
    decision_pool: PoolName
    factors: dict[str, int]
    reasons: list[str] = Field(default_factory=list)
    model_name: str = ""


class AuctionEvent(BaseModel):
    event_type: str
    event_time: date | None = None
    source_url: str = ""
    description: str = ""
    price_text: str = ""


class AssetRecord(BaseModel):
    asset_id: str
    title: str
    debtor: str = ""
    city: str = ""
    district: str = ""
    source_platform: str = ""
    source_url: str = ""
    disposal_agency: str = ""
    amount_text: str = ""
    raw_text: str = ""
    detail_text: str = ""
    detail_sections: list[dict[str, str]] = Field(default_factory=list)
    asset_tags: list[dict[str, str]] = Field(default_factory=list)
    extracted_summary: str = ""
    collateral_detail: str = ""
    attachment_summaries: list[dict[str, str]] = Field(default_factory=list)
    pool: PoolName = "scan"
    coarse_filter: CoarseFilterResult | None = None
    screening_score: ScreeningScore | None = None
    interested: bool = False
    announcement_count: int = 1
    auction_count: int = 0
    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    latest_notice_date: date | None = None
    latest_auction_time: date | None = None
    is_price_drop: bool = False
    is_split_sale: bool = False
    is_relisted: bool = False
    auction_history: list[AuctionEvent] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    node_models: dict[str, str] = Field(default_factory=dict)


class WorkflowResult(BaseModel):
    raw_count: int
    scan_count: int
    initial_count: int
    trackable_count: int
    review_count: int
    archive_count: int
    alerts: list[str]
    records: list[AssetRecord]
    model_plan: dict[str, dict[str, str | bool]] = Field(default_factory=dict)
