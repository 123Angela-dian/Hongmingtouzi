from __future__ import annotations

import hashlib
import re

from .model_config import model_for_node
from .models import CoarseFilterResult, RawNotice, ScreeningScore


REAL_ESTATE_TERMS = [
    "土地", "房产", "不动产", "住宅", "商业", "商铺", "商办", "办公",
    "厂房", "在建工程", "建筑物", "车位", "酒店", "公寓", "项目",
]

SECURITY_TERMS = ["抵押", "质押", "担保", "保证", "查封", "冻结"]
EQUITY_TERMS = ["股权", "上市公司", "股票", "收费权", "应收账款", "租金", "电费收费权"]
DISPOSAL_TERMS = ["拍卖", "竞价", "挂牌", "招商", "处置", "转让", "变卖", "二拍", "流拍", "重整"]
DEFECT_TERMS = ["查封", "破产", "施工优先权", "长租约", "欠费", "无法分割", "办证", "瑕疵", "执行"]
SPLIT_TERMS = ["拆分", "分拆", "单独", "分批", "部分转让"]
PRICE_DROP_TERMS = ["降价", "下调", "折价", "二拍", "变卖"]
RELIST_TERMS = ["重新挂牌", "再次挂牌", "二拍", "三拍", "流拍后", "恢复拍卖"]
KNOWN_CITIES = [
    "北京", "上海", "天津", "重庆", "广州", "深圳", "杭州", "南京", "苏州", "宁波", "厦门", "成都", "武汉", "西安",
    "长沙", "郑州", "青岛", "济南", "合肥", "福州", "南昌", "无锡", "常州", "佛山", "东莞", "珠海", "中山",
    "惠州", "温州", "嘉兴", "绍兴", "金华", "台州", "湖州", "南通", "扬州", "镇江", "泰州", "徐州", "盐城",
    "泉州", "漳州", "大连", "沈阳", "长春", "哈尔滨", "石家庄", "太原", "呼和浩特", "南宁", "昆明", "贵阳",
    "海口", "兰州", "银川", "西宁", "乌鲁木齐", "拉萨", "唐山", "廊坊", "保定", "洛阳", "宜昌", "襄阳",
    "芜湖", "赣州", "烟台", "潍坊", "临沂", "淄博", "济宁", "南阳", "珠海", "汕头", "湛江", "肇庆",
]
KNOWN_REGIONS = [
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北",
    "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾", "内蒙古", "广西", "西藏",
    "宁夏", "新疆", "香港", "澳门",
]
CITY_BAD_TERMS = ("有限公司", "股份", "上市公司", "城市", "都市", "市值", "市场", "市政", "市级", "市民")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def stable_asset_id(notice: RawNotice) -> str:
    identity = "|".join([
        normalize_text(notice.debtor),
        normalize_text(notice.city),
        normalize_text(notice.title)[:80],
        normalize_text(notice.raw_text)[:180],
    ])
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]


def contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def matched_terms(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if term in text]


def extract_city_from_text(*parts: str) -> str:
    text = " ".join(part or "" for part in parts)
    compact = normalize_text(text)
    if not compact:
        return ""
    for city in KNOWN_CITIES:
        if city in compact:
            return city
    for match in re.finditer(r"([\u4e00-\u9fff]{2,8}(?:市|自治州|地区|盟))", compact):
        city = match.group(1)
        if any(term in city for term in CITY_BAD_TERMS):
            continue
        if city.startswith(("关于", "债务人", "抵押物", "质押物", "资产包", "标的物")):
            continue
        return city
    for region in KNOWN_REGIONS:
        if region in compact:
            return region
    return ""


def coarse_filter(notice: RawNotice) -> CoarseFilterResult:
    text = f"{notice.title} {notice.raw_text}"
    reasons: list[str] = []
    asset_types: list[str] = []
    risk_tags: list[str] = []

    if contains_any(text, REAL_ESTATE_TERMS):
        reasons.append("包含土地、房产、在建工程、商业/办公等实物资产线索")
        asset_types.extend(matched_terms(text, REAL_ESTATE_TERMS))
    if contains_any(text, EQUITY_TERMS):
        reasons.append("包含股权、上市公司股票、收费权或应收账款等权益线索")
        asset_types.extend(matched_terms(text, EQUITY_TERMS))
    if contains_any(text, SECURITY_TERMS):
        reasons.append("包含抵押、质押、担保、查封等增信或限制信息")
        risk_tags.extend(matched_terms(text, SECURITY_TERMS))
    if contains_any(text, DISPOSAL_TERMS):
        reasons.append("处置阶段明确，具备持续追踪价值")

    passed = bool(asset_types) and ("债权" in text or contains_any(text, DISPOSAL_TERMS))
    return CoarseFilterResult(
        passed=passed,
        reasons=reasons if passed else reasons + ["未满足初筛：缺少可追踪资产或处置线索"],
        asset_types=sorted(set(asset_types)),
        risk_tags=sorted(set(risk_tags)),
        model_name=model_for_node("coarse_filter_node").model,
    )


def detailed_screening(notice: RawNotice) -> ScreeningScore:
    text = f"{notice.title} {notice.raw_text}"
    factors = {
        "实物资产强度": 25 if contains_any(text, REAL_ESTATE_TERMS) else 0,
        "抵押质押清晰度": min(20, 8 * len(set(matched_terms(text, SECURITY_TERMS)))),
        "城市与区位": 15 if notice.city or re.search(r"(上海|北京|深圳|广州|杭州|南京|苏州|宁波|厦门|成都|重庆|天津)", text) else 5,
        "处置进展": 15 if contains_any(text, DISPOSAL_TERMS) else 5,
        "权益控制价值": 10 if contains_any(text, EQUITY_TERMS) else 0,
        "瑕疵复杂度": min(10, 4 * len(set(matched_terms(text, DEFECT_TERMS)))),
        "信息完整度": 5 if notice.debtor and (notice.source_url or notice.notice_date) else 2,
    }
    total = int(sum(factors.values()))
    if total >= 70:
        pool = "trackable"
    elif total >= 50:
        pool = "review"
    else:
        pool = "archive"
    reasons = [
        f"{name}+{score}" for name, score in factors.items() if score > 0
    ]
    return ScreeningScore(
        total_score=total,
        decision_pool=pool,
        factors=factors,
        reasons=reasons,
        model_name=model_for_node("detailed_screening_node").model,
    )


def detect_update_events(previous_text: str, new_text: str) -> list[str]:
    text = f"{previous_text} {new_text}"
    events: list[str] = []
    if contains_any(text, PRICE_DROP_TERMS):
        events.append("price_drop_or_later_round")
    if contains_any(text, SPLIT_TERMS):
        events.append("split_sale")
    if contains_any(text, RELIST_TERMS):
        events.append("relisted")
    if contains_any(text, ["二拍", "三拍", "再次拍卖", "重新拍卖"]):
        events.append("new_auction_round")
    return sorted(set(events))
