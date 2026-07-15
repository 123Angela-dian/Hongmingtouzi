from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    name: str
    source_type: str
    url: str
    enabled: bool = True
    max_pages: int = 3
    page_size: int = 25
    notes: str = ""


DEFAULT_SOURCES = [
    SourceSpec(
        source_id="citic_amc_asset_disposal",
        name="中信金融资产处置公告",
        source_type="amc_notice",
        url="https://www.famc.citic/ywjs/ywdt/zcczxx/index.shtml",
        max_pages=5,
        page_size=25,
        notes="AMC asset disposal notices; suitable for debt transfer and collateral extraction.",
    ),
    SourceSpec(
        source_id="cinda_amc_asset_disposal",
        name="信达资产处置公告",
        source_type="amc_notice",
        url="https://www.cinda.com.cn/home/pc/cn/xdjt/qykhfw/blzcjy/zcczggjk/index.shtml",
        max_pages=4,
        page_size=15,
        notes="AMC asset disposal notices; suitable for recurring scan.",
    ),
    SourceSpec(
        source_id="ali_auction",
        name="阿里法拍",
        source_type="judicial_auction",
        url="https://sf.taobao.com/",
        max_pages=1,
        page_size=25,
        notes="Judicial auction platform; needs query and anti-duplication by asset address/case.",
    ),
    SourceSpec(
        source_id="jd_auction",
        name="京东法拍",
        source_type="judicial_auction",
        url="https://auction.jd.com/sifa.html",
        max_pages=1,
        page_size=25,
        notes="Judicial auction platform; needs query and status tracking.",
    ),
]
