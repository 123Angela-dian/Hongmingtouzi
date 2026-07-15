from __future__ import annotations

import json
import os
import csv
import base64
import hashlib
import re
import time
from contextlib import nullcontext
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Dict, Iterable

from docx import Document
from openpyxl import load_workbook
import streamlit as st
from dotenv import load_dotenv

from ocr import PaddleOCRError, run_paddleocr
from logger_config import LOG_FILE, get_logger
from state import ProjectState, empty_state


load_dotenv(override=True)
logger = get_logger("app")

from workflow import _extract_json_object, _invoke_brain, compiled_graph, structure_markdown_to_state

_smith_project = os.getenv("LANGSMITH_PROJECT") or os.getenv("LANGCHAIN_PROJECT") or "困境资产X光机_Demo"
os.environ["LANGCHAIN_PROJECT"] = _smith_project
os.environ["LANGSMITH_PROJECT"] = _smith_project
_no_proxy_hosts = "api.smith.langchain.com,smith.langchain.com"
os.environ["NO_PROXY"] = ",".join(filter(None, [os.getenv("NO_PROXY", ""), _no_proxy_hosts]))
os.environ["no_proxy"] = ",".join(filter(None, [os.getenv("no_proxy", ""), _no_proxy_hosts]))
if os.getenv("LANGCHAIN_API_KEY") and "请填写" not in os.getenv("LANGCHAIN_API_KEY", ""):
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = os.getenv("LANGCHAIN_API_KEY", "")
    os.environ["LANGSMITH_API_KEY"] = os.getenv("LANGCHAIN_API_KEY", "")
else:
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"


SAMPLE_DATA: Dict[str, str] = {
    "equity_and_history_data": """
## 1. 股权与历史沿革数据

- 项目公司：海南陵水海岸文旅置业有限公司，注册资本 3.2 亿元。
- 历史沿革：2018 年取得陵水滨海新区商住综合体项目公司 100% 股权；2020 年引入 A 基金增资 1.1 亿元。
- 股权结构：原实控人陈某控制 61%，A 基金名义持股 24%，地方平台公司持股 15%。
- 控制事项：工商登记法定代表人仍为陈某亲属；营业执照正副本由原管理团队保管。
- 印章事项：项目公司公章、财务章、网银 U 盾未完成交接，存在公章控制权争议。
- 历史瑕疵：A 基金增资款中 4000 万元由原实控人关联方过桥垫付，存在股权代持和增资瑕疵争议。
""",
    "financial_and_cost_data": """
## 2. 货值与开发成本数据

- 项目规划：总计容建面 18.6 万平方米，已建未售 6.2 万平方米，在建 8.4 万平方米，未开工 4.0 万平方米。
- 管理层口径总货值：约 19.8 亿元，假设住宅均价 32,000 元/平方米、商业均价 42,000 元/平方米。
- 审慎折扣：周边近 6 个月实际成交住宅均价约 27,500 元/平方米，商业去化弱，建议商业货值打 65 折。
- 已投入成本：土地及前期 5.4 亿元，已发生成本 4.1 亿元。
- 复工建安成本：总包报送 3.6 亿元，第三方复核 4.25 亿元，另需营销、税费、财务费用合计约 1.15 亿元。
- 去化周期：乐观 18 个月，审慎 30 个月；若限价继续执行，现金回款峰值将后移。
""",
    "creditor_and_seizure_data": """
## 3. 金融机构债权与查封明细

- 第一顺位债权人：海口某银行陵水支行，本金 5.8 亿元，利息及罚息暂估 0.9 亿元。
- 抵押登记：项目土地使用权及一期在建工程已抵押给海口某银行。
- 其他债权：信托计划受让原施工垫资债权 2.1 亿元，与原股东借款形成交叉质押安排。
- 查封情况：陵水县法院首封项目公司部分银行账户；海口中院轮候查封项目土地；三亚中院冻结原实控人所持项目公司股权。
- 司法风险：不同法院之间处置口径不一致，第一顺位银行尚未出具债务重组同意函。
- 施工争议：总包方申请工程款优先受偿权，金额约 6800 万元。
""",
    "asset_and_mortgage_data": """
## 4. 资产明细与抵押物清册

- 抵押物 A：陵水滨海新区 72 亩商住土地，土地用途商住，剩余年限住宅 62 年、商业 32 年。
- 抵押物 B：一期在建工程 6 栋住宅楼，形象进度 72%，部分楼栋存在停工渗漏和设备老化。
- 抵押物 C：沿街商业 1.8 万平方米，招商去化慢，历史报价明显高于周边可比项目。
- 资产限制：土地及在建工程均处于抵押状态，存在轮候查封，处置需第一顺位银行和查封法院配合。
- 工程缺口：消防、人防、精装、园林及市政接口工程尚未完成，复工前需补做质量鉴定。
- 现金流判断：若住宅按 27,500 元/平方米销售、商业打折处置，扣除复工及税费后净回收安全垫偏薄。
""",
}


SUPPORTED_INPUT_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".txt",
    ".md",
    ".docx",
    ".xlsx",
    ".csv",
}

OCR_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
TEXT_EXTENSIONS = {".txt", ".md"}
OFFICE_EXTENSIONS = {".docx", ".xlsx"}
CSV_EXTENSIONS = {".csv"}

CSV_CATEGORY_CONFIG = {
    "legal": {
        "state_key": "creditor_and_seizure_data",
        "label": "法律数据",
        "keywords": ("法律", "legal"),
    },
    "finance": {
        "state_key": "financial_and_cost_data",
        "label": "经济财务数据",
        "keywords": ("经济", "财务", "finance", "financial"),
    },
    "asset": {
        "state_key": "asset_and_mortgage_data",
        "label": "资产数据",
        "keywords": ("资产", "asset"),
    },
    "other": {
        "state_key": "equity_and_history_data",
        "label": "其他/项目基础数据",
        "keywords": ("项目数据信息", "初步打标", "其他", "other"),
    },
}


def _init_session() -> None:
    if "project_state" not in st.session_state:
        st.session_state.project_state = empty_state()
    if "run_done" not in st.session_state:
        st.session_state.run_done = False
    if "event_log" not in st.session_state:
        st.session_state.event_log = []
    if "ocr_markdown" not in st.session_state:
        st.session_state.ocr_markdown = ""
    if "run_dir" not in st.session_state:
        st.session_state.run_dir = ""
    if "material_count" not in st.session_state:
        st.session_state.material_count = 0
    if "project_updated_at" not in st.session_state:
        st.session_state.project_updated_at = ""
    if "active_dimension" not in st.session_state:
        st.session_state.active_dimension = ""
    if "metric_result_cache" not in st.session_state:
        st.session_state.metric_result_cache = {}
    if "show_overall_report" not in st.session_state:
        st.session_state.show_overall_report = False
    if "show_report_framework" not in st.session_state:
        st.session_state.show_report_framework = False
    if "active_report_view" not in st.session_state:
        st.session_state.active_report_view = False
    if "active_report_chapter" not in st.session_state:
        st.session_state.active_report_chapter = ""
    if "report_chapter_cache" not in st.session_state:
        st.session_state.report_chapter_cache = {}


def _is_configured(value: str | None) -> bool:
    if not value:
        return False
    stripped = value.strip()
    return bool(stripped) and "请填写" not in stripped


def _api_status() -> tuple[str, str, str]:
    load_dotenv(override=True)
    eyes_ready = _is_configured(os.getenv("EYES_API_KEY"))
    brain_ready = _is_configured(os.getenv("BRAIN_API_KEY")) or _is_configured(os.getenv("OPENAI_API_KEY"))
    smith_ready = _is_configured(os.getenv("LANGCHAIN_API_KEY"))
    return (
        "已配置" if eyes_ready else "未配置",
        "在线" if brain_ready else "未配置",
        "在线" if smith_ready else "未配置",
    )


def _render_css() -> None:
    st.markdown(
        """
        <style>
        .stApp {
            background: #101418;
            color: #eef2f7;
        }
        header[data-testid="stHeader"] {
            display: none;
        }
        div[data-testid="stToolbar"] {
            display: none;
        }
        div[data-testid="stDecoration"] {
            display: none;
        }
        #MainMenu {
            display: none;
        }
        .stApp, .stMarkdown, .stText, .stCaptionContainer, label, p, span, div {
            color: #eef2f7;
        }
        section[data-testid="stSidebar"] {
            background: #121820;
            border-right: 1px solid rgba(148, 163, 184, 0.20);
        }
        section[data-testid="stSidebar"] * {
            color: #eef2f7;
        }
        .block-container {
            padding-top: 1.6rem;
            max-width: 1180px;
        }
        .main .block-container {
            background: #151b22;
            border: 1px solid rgba(148, 163, 184, 0.18);
            border-radius: 8px;
            padding-left: 1.6rem;
            padding-right: 1.6rem;
            padding-bottom: 2rem;
        }
        .x-header {
            border-bottom: 1px solid rgba(148, 163, 184, 0.20);
            background: transparent;
            border-radius: 8px;
            padding: 0 0 0.9rem 0;
            margin-bottom: 1.1rem;
        }
        .x-title {
            font-size: 1.45rem;
            font-weight: 720;
            margin: 0 0 0.35rem 0;
        }
        .x-subtitle {
            color: rgba(226, 232, 240, 0.72);
            font-size: 0.95rem;
            margin: 0;
        }
        h2, h3 {
            color: #f8fafc;
        }
        div[data-testid="stVerticalBlock"] > div:has(> div[data-testid="stMarkdownContainer"] h3) {
            background: #1b2430;
            border: 1px solid rgba(148, 163, 184, 0.20);
            border-radius: 8px;
            padding: 1rem 1rem 1.1rem 1rem;
            margin-top: 0.9rem;
        }
        .status-card {
            border: 1px solid rgba(148, 163, 184, 0.16);
            background: #151d28;
            border-radius: 8px;
            padding: 0.9rem 1rem;
            min-height: 92px;
        }
        .status-label {
            color: rgba(226, 232, 240, 0.66);
            font-size: 0.82rem;
            margin-bottom: 0.35rem;
        }
        .status-value {
            font-size: 1.25rem;
            font-weight: 720;
        }
        .patch-box {
            border: 1px solid rgba(148, 163, 184, 0.16);
            border-left: 3px solid #38bdf8;
            background: #202a36;
            padding: 0.78rem 0.9rem;
            margin-bottom: 0.55rem;
            border-radius: 6px;
            color: #e5edf8;
            line-height: 1.55;
        }
        .decision-panel {
            border: 1px solid rgba(148, 163, 184, 0.18);
            background: #202a36;
            border-radius: 8px;
            padding: 1rem;
            margin-top: 0.6rem;
        }
        .small-muted, .stCaptionContainer {
            color: rgba(226, 232, 240, 0.68);
        }
        div[data-testid="stTabs"] button {
            background: transparent;
            color: #dbe7f5;
        }
        div[data-testid="stTabs"] div[role="tablist"] {
            background: #202a36;
            border-radius: 8px;
            padding: 0.2rem;
        }
        div[data-testid="stTabs"] button[aria-selected="true"] {
            color: #ffffff;
            border-bottom-color: #38bdf8;
        }
        div[data-testid="stFileUploader"] {
            background: #151b22;
            border: 1px solid rgba(148, 163, 184, 0.18);
            border-radius: 8px;
            padding: 0.35rem;
        }
        div[data-testid="stFileUploader"] section {
            background: #151b22;
            border-color: rgba(148, 163, 184, 0.24);
        }
        div[data-testid="stFileUploader"] * {
            color: #eef2f7;
        }
        div[data-testid="stAlert"] {
            background: #151b22;
            color: #eef2f7;
            border: 1px solid rgba(148, 163, 184, 0.18);
            border-radius: 8px;
        }
        div[data-testid="stAlert"] * {
            color: #eef2f7;
        }
        div[data-testid="stMetric"] {
            background: transparent;
        }
        div[data-testid="stMetric"] * {
            color: #eef2f7;
        }
        .stButton button {
            border-radius: 6px;
            color: #ffffff;
            border: 1px solid rgba(56, 189, 248, 0.45);
        }
        .stButton button:disabled {
            color: rgba(226, 232, 240, 0.45);
            background: #1a2028;
            border-color: rgba(148, 163, 184, 0.18);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _status_card(label: str, value: str, detail: str) -> None:
    st.markdown(
        f"""
        <div class="status-card">
            <div class="status-label">{escape(label)}</div>
            <div class="status-value">{escape(value)}</div>
            <div class="small-muted">{escape(detail)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _section_header(step: str, title: str, copy: str) -> None:
    st.markdown(
        f"""
        <div class="section-shell">
            <div class="step-kicker">{escape(step)}</div>
            <div class="section-title">{escape(title)}</div>
            <div class="section-copy">{escape(copy)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _patch_list(title: str, patches: list[str]) -> None:
    st.markdown(f"##### {title}")
    if not patches:
        st.caption("等待节点输出。")
        return
    for item in patches:
        with st.container(border=True):
            st.markdown(item)


def _risk_level_for_patch(text: str) -> str:
    high_terms = ("公章", "控制权", "第一顺位", "查封", "轮候", "交叉质押", "红线")
    medium_terms = ("代持", "纠纷", "复工", "建安", "估值", "货值", "去化", "税费", "抵押")
    if any(term in text for term in high_terms):
        return "高风险"
    if any(term in text for term in medium_terms):
        return "中风险"
    return "关注"


def _split_patch_parts(text: str) -> tuple[str, str]:
    for sep in ("：", ":", "。", "；", ";"):
        if sep in text:
            head, tail = text.split(sep, 1)
            return head.strip(" -*#"), tail.strip()
    return text[:28].strip(" -*#"), text


def _topic_key_metric(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    patterns = [
        r"\d+(?:\.\d+)?\s*(?:亿元|亿|万元|万|元/平方米|元/㎡|万平方米|万㎡|平方米|㎡|亩|%|个月|折)",
        r"(?:高风险|中风险|低风险|待确认|需复核|口径冲突|安全垫[^，。；]*)",
    ]
    hits: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, cleaned):
            value = match.group(0).strip()
            if value and value not in hits:
                hits.append(value)
            if len(hits) >= 3:
                break
        if len(hits) >= 3:
            break
    return " / ".join(hits) if hits else _short_text(cleaned, 34)


def _topic_review_item(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    review_keywords = ["待确认", "需复核", "需补充", "缺少", "缺口", "冲突", "不一致", "尚未", "未提供", "无法"]
    sentences = [item.strip() for item in re.split(r"[。；;]", cleaned) if item.strip()]
    for sentence in sentences:
        if any(keyword in sentence for keyword in review_keywords):
            return _short_text(sentence, 46)
    return "复核证据来源、金额口径和对 PCS 结论的影响"


def _render_agent_topic_board(title: str, patches: list[str], empty_text: str) -> None:
    st.markdown(f"##### {title}")
    if not patches:
        st.caption(empty_text)
        return

    for index, patch in enumerate(patches, start=1):
        topic, detail = _split_patch_parts(str(patch))
        level = _risk_level_for_patch(str(patch))
        with st.container(border=True):
            st.markdown(f"<div class='topic-title'>专题 {index}：{escape(topic or title)}</div>", unsafe_allow_html=True)
            if level == "高风险":
                st.error(level)
            elif level == "中风险":
                st.warning(level)
            else:
                st.info(level)

            st.markdown(f"<div class='topic-body'>{escape(detail or patch)}</div>", unsafe_allow_html=True)
            st.caption(f"证据强度：已识别 | 处置优先级：{'高' if level == '高风险' else '中'} | 状态：待复核")
            st.progress(0.85 if level == "高风险" else 0.62 if level == "中风险" else 0.42)


def _render_workbench_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --ms-blue: #002b5e;
            --ms-blue-light: #00438a;
            --bg-gray: #f4f5f7;
            --border-gray: #e0e0e0;
            --text-main: #333333;
            --text-muted: #666666;
            --risk-high: #8b0000;
            --risk-medium: #d97706;
            --white: #ffffff;
        }
        .stApp {
            background: var(--bg-gray) !important;
            color: var(--text-main) !important;
            font-family: "Helvetica Neue", Helvetica, Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
        }
        .stApp, .stMarkdown, .stText, .stCaptionContainer, label, p, span, div {
            color: var(--text-main);
        }
        .block-container {
            max-width: 1440px !important;
            padding: 76px 24px 42px !important;
        }
        .main .block-container {
            background: transparent !important;
            border: 0 !important;
            border-radius: 0 !important;
        }
        section[data-testid="stSidebar"] {
            background: #fff !important;
            border-right: 1px solid var(--border-gray) !important;
        }
        section[data-testid="stSidebar"] * {
            color: var(--text-main) !important;
        }
        h1, h2, h3, h4, h5 {
            color: var(--ms-blue) !important;
            letter-spacing: 0;
        }
        div[data-testid="stTabs"] div[role="tablist"] {
            background: #fff !important;
            border: 1px solid var(--border-gray);
            border-radius: 2px !important;
            padding: 4px !important;
        }
        div[data-testid="stTabs"] button[aria-selected="true"] {
            color: #fff !important;
            background: var(--ms-blue) !important;
        }
        div[data-testid="stAlert"], div[data-testid="stFileUploader"], div[data-testid="stFileUploader"] section {
            background: #fff !important;
            border-color: var(--border-gray) !important;
            border-radius: 2px !important;
        }
        div[data-testid="stAlert"] *, div[data-testid="stFileUploader"] *, div[data-testid="stMetric"] * {
            color: var(--text-main) !important;
        }
        .stButton button {
            border-radius: 2px !important;
            border: 1px solid var(--ms-blue) !important;
            background: var(--ms-blue) !important;
            color: #fff !important;
            font-size: 12px !important;
            font-weight: 600 !important;
        }
        .stButton button *, .stButton button p, .stButton button span {
            color: #fff !important;
        }
        .stButton button:hover {
            background: var(--ms-blue-light) !important;
        }
        .stButton button:disabled {
            background: #e5e7eb !important;
            color: #9ca3af !important;
            border-color: #d1d5db !important;
        }
        .stButton button:disabled *, .stButton button:disabled p, .stButton button:disabled span {
            color: #9ca3af !important;
        }
        .credit-header {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            height: 56px;
            z-index: 1000;
            background: var(--ms-blue);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 24px;
            box-shadow: 0 1px 2px rgba(0,0,0,.12);
        }
        .credit-header * { color: #fff !important; }
        .credit-header-left, .credit-header-right {
            display: flex;
            align-items: center;
            gap: 16px;
            min-width: 0;
        }
        .credit-logo {
            width: 138px;
            height: 38px;
            background: #fff;
            border-radius: 2px;
            padding: 5px 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-right: 1px solid rgba(255,255,255,.24);
            margin-right: 2px;
        }
        .credit-logo img {
            display: block;
            width: 100%;
            height: auto;
            max-height: 28px;
            object-fit: contain;
        }
        .credit-project {
            font-size: 13px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .credit-search, .mode-chip {
            border: 1px solid rgba(255,255,255,.28);
            background: rgba(255,255,255,.10);
            border-radius: 2px;
            padding: 5px 10px;
            font-size: 12px;
            white-space: nowrap;
        }
        .overview-bar {
            display: flex;
            background: #fff;
            border: 1px solid var(--border-gray);
            margin-bottom: 24px;
            overflow-x: auto;
        }
        .overview-item {
            min-width: 150px;
            padding: 16px 24px;
            border-right: 1px solid var(--border-gray);
        }
        .overview-label {
            display: block;
            color: var(--text-muted) !important;
            font-size: 11px;
            margin-bottom: 5px;
        }
        .overview-value {
            display: block;
            color: var(--ms-blue) !important;
            font-size: 18px;
            font-weight: 700;
            line-height: 1.25;
        }
        .overview-value.alert { color: var(--risk-high) !important; }
        .card, .report-shell {
            background: #fff;
            border: 1px solid var(--border-gray);
            border-radius: 2px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 1px 2px rgba(0,0,0,.02);
        }
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            border-bottom: 1px solid var(--border-gray);
            padding-bottom: 12px;
            margin-bottom: 16px;
        }
        .card-title {
            color: var(--ms-blue) !important;
            font-size: 16px;
            font-weight: 700;
        }
        .dimension-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 20px;
        }
        .dim-card {
            min-height: 222px;
            display: flex;
            flex-direction: column;
        }
        .dim-card-shell {
            min-height: 222px;
        }
        .dim-summary {
            font-size: 13px;
            line-height: 1.55;
            flex: 1;
            color: var(--text-main) !important;
        }
        .workbench-dim-title {
            font-size: 16px;
            font-weight: 700;
            color: var(--ms-blue) !important;
            margin-bottom: 4px;
        }
        .workbench-section-title {
            font-size: 16px;
            font-weight: 700;
            color: var(--ms-blue) !important;
            margin: 18px 0 12px;
        }
        .dimension-title {
            font-size: 24px;
            font-weight: 700;
            color: var(--ms-blue) !important;
            margin-bottom: 4px;
        }
        .dimension-one-liner {
            font-size: 12px;
            color: var(--text-muted) !important;
            margin-bottom: 14px;
        }
        .judgement-title {
            font-size: 18px;
            font-weight: 700;
            color: var(--ms-blue) !important;
            line-height: 1.45;
            margin-bottom: 10px;
        }
        .topic-title {
            font-size: 14px;
            font-weight: 700;
            color: var(--ms-blue) !important;
            margin-bottom: 8px;
        }
        .topic-body {
            font-size: 12.5px;
            line-height: 1.6;
        }
        .detail-source-text {
            font-size: 12.5px;
            line-height: 1.55;
        }
        .detail-metric-card {
            min-height: 184px;
            height: 184px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            background: #fff;
            border: 1px solid var(--border-gray);
            border-radius: 2px;
            padding: 16px 14px;
            box-shadow: 0 1px 2px rgba(0,0,0,.02);
            overflow: hidden;
        }
        .detail-metric-label {
            color: var(--text-muted) !important;
            font-size: 12px;
            line-height: 1.25;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .detail-metric-value {
            color: var(--ms-blue) !important;
            font-size: 24px;
            font-weight: 700;
            line-height: 1.25;
            margin: 10px 0;
            word-break: break-word;
        }
        .detail-metric-desc {
            color: var(--text-muted) !important;
            font-size: 12px;
            line-height: 1.55;
            display: -webkit-box;
            -webkit-line-clamp: 3;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        .topic-meta {
            margin-top: 12px;
            padding-top: 10px;
            border-top: 1px solid #f0f0f0;
        }
        .topic-meta-line {
            font-size: 13px;
            line-height: 1.65;
            color: var(--text-main) !important;
        }
        .topic-meta-label {
            color: var(--text-muted) !important;
            font-weight: 600;
        }
        .dim-slot {
            font-size: 11px;
            color: var(--text-muted) !important;
            margin-top: 10px;
            line-height: 1.45;
        }
        .dim-data-list {
            list-style: none;
            padding: 0;
            margin: 12px 0 0;
            font-size: 12px;
        }
        .dim-data-list li {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            padding: 6px 0;
            border-bottom: 1px solid #f0f0f0;
        }
        .dim-data-list li:last-child { border-bottom: none; }
        .dim-data-label { color: var(--text-muted) !important; }
        .dim-data-value {
            font-weight: 600;
            color: var(--text-main) !important;
            text-align: right;
        }
        .dim-footer {
            border-top: 1px solid var(--border-gray);
            padding-top: 12px;
            margin-top: 14px;
            display: flex;
            justify-content: space-between;
            gap: 12px;
            font-size: 12px;
        }
        .tag {
            display: inline-block;
            padding: 2px 6px;
            font-size: 11px;
            border-radius: 2px;
            font-weight: 700;
            white-space: nowrap;
        }
        .tag.high { background: #fde8e8; color: var(--risk-high) !important; border: 1px solid #f8b4b4; }
        .tag.medium { background: #fef3c7; color: var(--risk-medium) !important; border: 1px solid #fde68a; }
        .tag.status, .tag.low { background: #e1effe; color: var(--ms-blue) !important; border: 1px solid #c3ddfd; }
        .process-step {
            position: relative;
            border-left: 2px solid var(--border-gray);
            padding-left: 18px;
            padding-bottom: 16px;
            font-size: 12px;
            line-height: 1.6;
        }
        .process-step:before {
            content: "";
            position: absolute;
            left: -6px;
            top: 4px;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: var(--ms-blue);
            border: 2px solid #fff;
        }
        .process-title {
            font-weight: 700;
            color: var(--ms-blue) !important;
            margin-bottom: 3px;
        }
        .patch-box {
            border: 1px solid var(--border-gray) !important;
            border-left: 3px solid var(--ms-blue) !important;
            background: #fff !important;
            color: var(--text-main) !important;
            border-radius: 2px !important;
            padding: 10px 12px !important;
            margin-bottom: 8px !important;
            line-height: 1.6;
            font-size: 12px;
        }
        .decision-panel {
            background: #fff !important;
            border: 1px solid var(--border-gray) !important;
            border-top: 3px solid var(--ms-blue) !important;
            border-radius: 2px !important;
            padding: 20px !important;
        }
        .material-note {
            border: 1px dashed var(--border-gray);
            background: #fff;
            padding: 22px;
            text-align: center;
            margin-bottom: 20px;
        }
        .project-definition-card {
            background: #fff;
            border: 1px solid var(--border-gray);
            border-radius: 2px;
            padding: 18px 18px 12px;
            margin-bottom: 20px;
            box-shadow: 0 1px 2px rgba(0,0,0,.02);
        }
        .project-definition-card .card-header {
            margin-bottom: 10px;
        }
        .project-definition-list {
            list-style: none;
            padding: 0;
            margin: 0;
            font-size: 12.5px;
        }
        .project-definition-list li {
            padding: 10px 0;
            border-bottom: 1px solid #f0f0f0;
        }
        .project-definition-list li:last-child { border-bottom: none; }
        .project-definition-label {
            display: block;
            color: var(--text-muted) !important;
            font-size: 11px;
            margin-bottom: 4px;
        }
        .project-definition-value {
            display: block;
            color: var(--text-main) !important;
            font-size: 14px;
            font-weight: 700;
            line-height: 1.45;
        }
        .project-definition-desc {
            display: block;
            color: var(--text-muted) !important;
            margin-top: 4px;
            line-height: 1.5;
        }
        .compact-list {
            padding-left: 18px;
            font-size: 12px;
            line-height: 1.8;
        }
        .overall-report-panel {
            background: #fff;
            border: 1px solid var(--border-gray);
            border-radius: 2px;
            padding: 18px 20px;
            margin: 10px 0 20px;
            box-shadow: 0 1px 2px rgba(0,0,0,.02);
        }
        .overall-report-head {
            display: grid;
            grid-template-columns: 1.4fr repeat(3, minmax(110px, 1fr));
            gap: 12px;
            margin-bottom: 16px;
        }
        .overall-report-summary {
            border-left: 3px solid var(--ms-blue);
            padding-left: 12px;
            min-height: 74px;
            display: flex;
            flex-direction: column;
            justify-content: center;
        }
        .overall-report-summary strong {
            color: var(--ms-blue) !important;
            font-size: 15px;
            margin-bottom: 6px;
        }
        .overall-report-summary span {
            color: var(--text-muted) !important;
            font-size: 12px;
            line-height: 1.6;
        }
        .report-stat {
            border: 1px solid var(--border-gray);
            background: #fafafa;
            padding: 12px;
        }
        .report-stat span {
            display: block;
            color: var(--text-muted) !important;
            font-size: 11px;
            margin-bottom: 6px;
        }
        .report-stat strong {
            display: block;
            color: var(--ms-blue) !important;
            font-size: 20px;
            line-height: 1.2;
        }
        .report-stat em {
            display: block;
            color: #999 !important;
            font-size: 11px;
            font-style: normal;
            margin-top: 6px;
        }
        .report-chapter-card {
            border: 1px solid var(--border-gray);
            background: #fff;
            padding: 14px;
            min-height: 154px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            margin-bottom: 10px;
        }
        .report-chapter-title {
            color: var(--ms-blue) !important;
            font-size: 14px;
            font-weight: 700;
            margin-bottom: 8px;
            line-height: 1.35;
        }
        .report-chapter-desc {
            color: var(--text-muted) !important;
            font-size: 12px;
            line-height: 1.6;
        }
        .chapter-meta {
            display: flex;
            justify-content: space-between;
            gap: 8px;
            align-items: center;
            margin-top: 12px;
        }
        .chapter-progress {
            flex: 1;
            height: 5px;
            background: #edf2f7;
            overflow: hidden;
        }
        .chapter-progress span {
            display: block;
            height: 100%;
            background: var(--ms-blue);
        }
        .breadcrumb {
            margin: 0 0 16px;
            font-size: 12px;
            color: var(--text-muted) !important;
        }
        .report-detail-card {
            background: #fff;
            border: 1px solid var(--border-gray);
            border-radius: 2px;
            padding: 32px 40px;
            margin-bottom: 20px;
            box-shadow: 0 1px 2px rgba(0,0,0,.02);
        }
        .report-detail-header {
            border-bottom: 2px solid var(--ms-blue);
            padding-bottom: 16px;
            margin-bottom: 24px;
            display: flex;
            justify-content: space-between;
            gap: 16px;
            align-items: center;
        }
        .report-detail-title {
            color: var(--ms-blue) !important;
            font-size: 22px;
            font-weight: 700;
            line-height: 1.35;
        }
        .report-section-title {
            color: var(--ms-blue) !important;
            font-size: 14px;
            font-weight: 700;
            border-left: 3px solid var(--ms-blue);
            padding-left: 8px;
            margin: 0 0 12px;
        }
        .empty-report {
            min-height: 360px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--text-muted) !important;
            border: 1px dashed var(--border-gray);
            background: #fafafa;
            font-size: 14px;
            margin-top: 24px;
            text-align: center;
            padding: 24px;
        }
        .generated-report-body {
            border: 1px solid var(--border-gray);
            background: #fff;
            padding: 20px;
            margin-top: 24px;
        }
        .report-insight-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
            margin: 14px 0 18px;
        }
        .report-insight-card {
            border: 1px solid var(--border-gray);
            background: #fafafa;
            padding: 12px;
            min-height: 92px;
        }
        .report-insight-card strong {
            display: block;
            color: var(--ms-blue) !important;
            font-size: 13px;
            margin-bottom: 8px;
        }
        .report-insight-card span {
            color: var(--text-muted) !important;
            font-size: 12px;
            line-height: 1.6;
        }
        .report-content-list {
            padding-left: 18px;
            margin: 8px 0 16px;
            font-size: 13px;
            line-height: 1.8;
        }
        .report-content-list li {
            margin-bottom: 6px;
        }
        .report-mini-table {
            width: 100%;
            border-collapse: collapse;
            margin: 12px 0 18px;
            font-size: 12px;
        }
        .report-mini-table th,
        .report-mini-table td {
            border: 1px solid var(--border-gray);
            padding: 8px 10px;
            vertical-align: top;
            text-align: left;
        }
        .report-mini-table th {
            background: #fafafa;
            color: var(--text-muted) !important;
        }
        .stButton > button[kind="primary"], .stButton > button[data-testid="baseButton-primary"] {
            background: var(--ms-blue) !important;
            color: #fff !important;
            border-color: var(--ms-blue) !important;
        }
        .stButton > button {
            border-radius: 2px !important;
            font-size: 12px !important;
            font-weight: 600 !important;
        }
        .small-muted, .stCaptionContainer { color: var(--text-muted) !important; }
        @media (max-width: 980px) {
            .dimension-grid { grid-template-columns: 1fr; }
            .overall-report-head { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .credit-header-right { display: none; }
            .credit-project { max-width: 58vw; }
        }
        @media (max-width: 720px) {
            .overall-report-head { grid-template-columns: 1fr; }
            .report-insight-grid { grid-template-columns: 1fr; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _asset_data_uri(path: str) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return ""
    mime = "image/png" if file_path.suffix.lower() == ".png" else "application/octet-stream"
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _render_top_header(project_title: str) -> None:
    logo_src = _asset_data_uri("assets/hongming-logo-transparent.png")
    logo_html = f'<img src="{logo_src}" alt="弘明投资">' if logo_src else "Homing"
    st.markdown(
        f"""
        <div class="credit-header">
            <div class="credit-header-left">
                <div class="credit-logo">{logo_html}</div>
                <div class="credit-project">{escape(project_title)}</div>
            </div>
            <div class="credit-header-right">
                <div class="credit-search">搜索主体、合同、资产、金额...</div>
                <div class="mode-chip">摘要 / 详情</div>
                <div style="font-size:12px;">当前用户：投资部</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _short_text(value: str, limit: int = 150) -> str:
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        return "等待材料解析后生成该维度摘要。"
    return cleaned if len(cleaned) <= limit else f"{cleaned[:limit]}..."


def _short_event(value: object, limit: int = 180) -> str:
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        return "无事件内容"
    return cleaned if len(cleaned) <= limit else f"{cleaned[:limit]}..."


def _combined_state_text(state: ProjectState, *keys: str) -> str:
    return "\n".join(str(state.get(key, "") or "") for key in keys)


def _first_match(text: str, patterns: list[str], default: str = "待识别") -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            for group in match.groups():
                if group:
                    return group.strip()
    return default


def _count_keywords(text: str, keywords: list[str]) -> str:
    count = sum(1 for keyword in keywords if keyword in text)
    return f"{count}项" if count else "待识别"


METRIC_CONFIGS: dict[str, dict[str, object]] = {
    "land_area": {
        "label": "土地面积",
        "dimension": "asset",
        "keywords": ["土地面积", "土地", "亩", "用地面积", "宗地", "地块"],
        "fields": ["面积", "土地", "抵押物", "资产名称", "资产描述"],
        "units": ["亩", "平方米", "㎡", "公顷"],
    },
    "building_area": {
        "label": "建筑面积",
        "dimension": "asset",
        "keywords": ["建筑面积", "建面", "计容建面", "总建面", "在建工程", "商业面积", "住宅面积"],
        "fields": ["建筑面积", "建面", "面积", "规划指标", "资产描述"],
        "units": ["平方米", "㎡", "万平方米", "万㎡"],
    },
    "mortgage_value": {
        "label": "抵押物价值",
        "dimension": "asset",
        "keywords": ["抵押物价值", "评估值", "抵押价值", "估值", "市场价值"],
        "fields": ["评估值", "价值", "抵押物", "金额"],
        "units": ["元", "万元", "亿元", "亿"],
    },
    "disposable_amount": {
        "label": "可处置金额",
        "dimension": "asset",
        "keywords": ["可处置金额", "可处置价值", "净回收", "处置回收", "折价处置", "安全垫"],
        "fields": ["处置", "回收", "价值", "金额", "测算"],
        "units": ["元", "万元", "亿元", "亿"],
    },
    "total_gmv": {
        "label": "总货值",
        "dimension": "economic",
        "keywords": ["总货值", "货值", "总销货值", "总可售货值", "销售货值"],
        "fields": ["货值", "金额", "测算", "口径"],
        "units": ["元", "万元", "亿元", "亿"],
    },
    "remaining_gmv": {
        "label": "剩余货值",
        "dimension": "economic",
        "keywords": ["剩余货值", "未售货值", "可售货值", "已建未售", "未售"],
        "fields": ["货值", "未售", "剩余", "可售", "金额"],
        "units": ["元", "万元", "亿元", "亿"],
    },
    "collection_amount": {
        "label": "回款",
        "dimension": "economic",
        "keywords": ["回款", "现金回款", "销售回款", "回款峰值", "现金流"],
        "fields": ["回款", "现金流", "金额", "周期"],
        "units": ["元", "万元", "亿元", "亿"],
    },
    "debt_principal": {
        "label": "债权本金",
        "dimension": "legal",
        "keywords": ["债权本金", "本金", "第一顺位", "金融债权", "债权人"],
        "fields": ["本金", "债权", "债权人", "顺位", "金额"],
        "units": ["元", "万元", "亿元", "亿"],
    },
    "debt_balance": {
        "label": "债权余额",
        "dimension": "legal",
        "keywords": ["债权余额", "本息", "余额", "利息", "罚息", "尚欠"],
        "fields": ["余额", "本息", "利息", "罚息", "债权", "金额"],
        "units": ["元", "万元", "亿元", "亿"],
    },
    "debt_net": {
        "label": "债权净额",
        "dimension": "legal",
        "keywords": ["债权净额", "净债权", "扣除", "清偿后", "净额"],
        "fields": ["净额", "债权", "扣除", "金额"],
        "units": ["元", "万元", "亿元", "亿"],
    },
    "first_lien_debt": {
        "label": "第一顺位债权",
        "dimension": "legal",
        "keywords": ["第一顺位", "首封", "优先债权", "抵押权人", "本金"],
        "fields": ["顺位", "债权", "本金", "抵押权人", "金额"],
        "units": ["元", "万元", "亿元", "亿"],
    },
    "cost_amount": {
        "label": "成本",
        "dimension": "financial",
        "keywords": ["成本", "复工成本", "建安成本", "已投入成本", "税费", "营销费用", "财务费用"],
        "fields": ["成本", "费用", "税费", "金额"],
        "units": ["元", "万元", "亿元", "亿"],
    },
    "clearance_rate": {
        "label": "清偿率",
        "dimension": "financial",
        "keywords": ["清偿率", "清偿", "覆盖率", "偿付率", "净回收", "债权"],
        "fields": ["清偿率", "清偿", "覆盖率", "回收", "债权"],
        "units": ["%", "比例"],
    },
    "mortgage_coverage": {
        "label": "抵押覆盖率",
        "dimension": "financial",
        "keywords": ["抵押覆盖率", "抵押覆盖", "抵押物价值", "债权本金", "覆盖率"],
        "fields": ["覆盖率", "抵押", "评估值", "债权", "本金"],
        "units": ["%", "比例"],
    },
}


DIMENSION_METRIC_KEYS: dict[str, list[str]] = {
    "asset": ["land_area", "building_area", "mortgage_value", "disposable_amount"],
    "economic": ["total_gmv", "remaining_gmv", "building_area", "collection_amount"],
    "legal": ["first_lien_debt", "debt_principal", "debt_balance", "debt_net"],
    "financial": ["cost_amount", "collection_amount", "clearance_rate", "mortgage_coverage"],
}


def _is_brain_ready() -> bool:
    return _is_configured(os.getenv("BRAIN_API_KEY")) or _is_configured(os.getenv("OPENAI_API_KEY"))


def _status_text(status: str) -> str:
    return {
        "identified": "已识别",
        "pending_identification": "待识别",
        "pending_calculation": "待测算",
        "conflict": "口径冲突",
    }.get(status, status or "待识别")


def _metric_display_value(result: dict) -> str:
    status = str(result.get("status") or "")
    value = str(result.get("value") or "").strip()
    unit = str(result.get("unit") or "").strip()
    if status in {"pending_identification", "pending_calculation", "conflict"} and not value:
        return _status_text(status)
    return f"{value}{unit}" if value and unit and unit not in value else (value or _status_text(status))


def _metric_desc(result: dict) -> str:
    confidence = result.get("confidence") or "low"
    source_count = len(result.get("sources") or [])
    basis = str(result.get("basis") or "").strip()
    calculation = str(result.get("calculation") or "").strip()
    parts = [f"{_status_text(str(result.get('status') or ''))} / 置信度：{confidence}", f"证据：{source_count} 条"]
    if calculation:
        parts.append(f"计算：{calculation[:80]}")
    elif basis:
        parts.append(basis[:80])
    return "；".join(parts)


def _state_text_evidence_rows(state: ProjectState) -> list[dict]:
    rows: list[dict] = []
    category_map = {
        "asset_and_mortgage_data": "asset",
        "financial_and_cost_data": "finance",
        "creditor_and_seizure_data": "legal",
        "equity_and_history_data": "other",
    }
    for key, category in category_map.items():
        text = str(state.get(key) or "")
        for index, line in enumerate([item.strip("-  \t") for item in text.splitlines() if item.strip()], start=1):
            if not line or line.startswith("#"):
                continue
            rows.append(
                {
                    "file": "结构化文本",
                    "sheet": category,
                    "row_index": index,
                    "column": key,
                    "text": line,
                    "category": category,
                    "date_version": "",
                }
            )
    return rows


def _all_evidence_rows(state: ProjectState) -> list[dict]:
    rows = state.get("csv_evidence_rows") or []
    return [dict(row) for row in rows] if rows else _state_text_evidence_rows(state)


def _score_evidence_row(row: dict, config: dict[str, object]) -> int:
    text = " ".join(str(row.get(key, "")) for key in ["column", "text", "category"])
    keywords = [str(item).lower() for item in config.get("keywords", [])]
    fields = [str(item).lower() for item in config.get("fields", [])]
    lowered = text.lower()
    score = 0
    score += sum(3 for item in keywords if item and item.lower() in lowered)
    score += sum(2 for item in fields if item and item.lower() in lowered)
    if str(row.get("category") or "") == str(config.get("dimension") or ""):
        score += 1
    return score


def _recall_metric_evidence(state: ProjectState, config: dict[str, object], limit: int = 16) -> list[dict]:
    scored: list[tuple[int, dict]] = []
    for row in _all_evidence_rows(state):
        score = _score_evidence_row(row, config)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in scored[:limit]]


def _extract_candidate_values(rows: list[dict]) -> list[dict]:
    candidates: list[dict] = []
    value_pattern = re.compile(
        r"(?P<value>-?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?P<unit>万平方米|万㎡|平方米|㎡|元/平方米|元/㎡|亿元|万元|亿|万|元|亩|个月|%|折)?"
    )
    for row in rows:
        text = str(row.get("text") or "")
        for match in value_pattern.finditer(text):
            value = match.group("value")
            unit = match.group("unit") or ""
            if not value:
                continue
            candidates.append(
                {
                    "value": value.replace(",", ""),
                    "unit": unit,
                    "raw": match.group(0),
                    "column": row.get("column", ""),
                    "row_index": row.get("row_index", ""),
                    "text": text,
                }
            )
    return candidates[:30]


def _empty_metric_result(metric_key: str, label: str, status: str, basis: str, sources: list[dict] | None = None) -> dict:
    return {
        "metric_key": metric_key,
        "label": label,
        "value": "",
        "unit": "",
        "status": status,
        "confidence": "low",
        "basis": basis,
        "calculation": "",
        "sources": sources or [],
        "conflicts": [],
        "need_manual_review": status != "identified",
    }


def _normalize_metric_result(metric_key: str, label: str, payload: dict, evidence_rows: list[dict]) -> dict:
    allowed_status = {"identified", "pending_identification", "pending_calculation", "conflict"}
    allowed_confidence = {"high", "medium", "low"}
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    normalized_sources = []
    for source in sources[:8]:
        if isinstance(source, dict):
            normalized_sources.append(
                {
                    "file": str(source.get("file") or ""),
                    "sheet": str(source.get("sheet") or ""),
                    "row_index": str(source.get("row_index") or ""),
                    "column": str(source.get("column") or ""),
                    "text": str(source.get("text") or "")[:300],
                }
            )
    if not normalized_sources and evidence_rows:
        normalized_sources = [
            {
                "file": str(row.get("file") or ""),
                "sheet": str(row.get("sheet") or ""),
                "row_index": str(row.get("row_index") or ""),
                "column": str(row.get("column") or ""),
                "text": str(row.get("text") or "")[:300],
            }
            for row in evidence_rows[:3]
        ]
    status = str(payload.get("status") or "pending_calculation")
    confidence = str(payload.get("confidence") or "low")
    return {
        "metric_key": metric_key,
        "label": str(payload.get("label") or label),
        "value": str(payload.get("value") or "").strip(),
        "unit": str(payload.get("unit") or "").strip(),
        "status": status if status in allowed_status else "pending_calculation",
        "confidence": confidence if confidence in allowed_confidence else "low",
        "basis": str(payload.get("basis") or "").strip(),
        "calculation": str(payload.get("calculation") or "").strip(),
        "sources": normalized_sources,
        "conflicts": payload.get("conflicts") if isinstance(payload.get("conflicts"), list) else [],
        "need_manual_review": bool(payload.get("need_manual_review", status != "identified")),
    }


def _metric_cache_key(metric_key: str, evidence_rows: list[dict], candidates: list[dict]) -> str:
    payload = {
        "metric_key": metric_key,
        "evidence": evidence_rows,
        "candidates": candidates,
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _ai_judge_metric(metric_key: str, config: dict[str, object], evidence_rows: list[dict], candidates: list[dict]) -> dict:
    label = str(config["label"])
    if not evidence_rows:
        return _empty_metric_result(metric_key, label, "pending_identification", "未从CSV/表格结构化证据中召回相关行。")
    if not candidates:
        return _empty_metric_result(metric_key, label, "pending_calculation", "已召回相关证据，但未抽取到可用于判断或计算的候选数值。", evidence_rows[:3])
    cache_key = _metric_cache_key(metric_key, evidence_rows, candidates)
    cache = st.session_state.get("metric_result_cache", {})
    if cache_key in cache:
        return cache[cache_key]
    if not _is_brain_ready():
        result = _empty_metric_result(
            metric_key,
            label,
            "pending_calculation",
            "已召回证据和候选数值，但未配置Brain模型，无法完成AI口径判断/计算。",
            evidence_rows[:3],
        )
        result["conflicts"] = candidates[:8]
        cache[cache_key] = result
        st.session_state.metric_result_cache = cache
        return result

    system_prompt = """
你是困境资产投决系统的首页指标口径判断器。
你只能基于用户提供的 CSV/表格证据行和候选数值判断，不能引入外部事实，不能从全量材料自由生成。
需要判断最终值、口径、冲突、是否需要合计/换算/相减/计算，并返回严格 JSON。
没有证据返回 pending_identification；有证据但缺少计算输入返回 pending_calculation；多口径冲突返回 conflict。
"""
    user_payload = json.dumps(
        {
            "metric_key": metric_key,
            "label": label,
            "metric_config": config,
            "evidence_rows": evidence_rows,
            "candidate_values": candidates,
            "output_schema": {
                "metric_key": "",
                "label": "",
                "value": "",
                "unit": "",
                "status": "identified / pending_identification / pending_calculation / conflict",
                "confidence": "high / medium / low",
                "basis": "",
                "calculation": "",
                "sources": [{"file": "", "sheet": "", "row_index": "", "column": "", "text": ""}],
                "conflicts": [],
                "need_manual_review": False,
            },
        },
        ensure_ascii=False,
    )
    try:
        payload = _extract_json_object(_invoke_brain(system_prompt, user_payload))
        result = _normalize_metric_result(metric_key, label, payload, evidence_rows)
    except Exception as exc:
        logger.warning("Metric AI judgement failed | metric=%s | error=%s", metric_key, exc)
        result = _empty_metric_result(metric_key, label, "pending_calculation", f"AI口径判断失败：{exc}", evidence_rows[:3])
        result["conflicts"] = candidates[:8]
    cache[cache_key] = result
    st.session_state.metric_result_cache = cache
    return result


def _metric_result(metric_key: str, state: ProjectState) -> dict:
    config = METRIC_CONFIGS[metric_key]
    evidence_rows = _recall_metric_evidence(state, config)
    candidates = _extract_candidate_values(evidence_rows)
    return _ai_judge_metric(metric_key, config, evidence_rows, candidates)


def _risk_class(score: int | None, has_data: bool) -> tuple[str, str]:
    if not has_data:
        return "status", "待解析"
    if score is None:
        return "medium", "待宣判"
    if score < 50:
        return "high", "高风险"
    if score < 75:
        return "medium", "中等风险"
    return "low", "条件通过"


def _render_overview_bar(has_data: bool, run_done: bool, score: int | None, material_count: int) -> None:
    risk_class, risk_label = _risk_class(score, has_data)
    updated_at = st.session_state.get("project_updated_at") or "--"
    version = "v1.0 Draft" if has_data else "未建档"
    pending = "0 项" if run_done else ("1 项" if has_data else "--")
    flow_status = "已宣判" if run_done else ("待扫描" if has_data else "待接入")
    st.markdown(
        f"""
        <div class="overview-bar">
            <div class="overview-item"><span class="overview-label">报告版本</span><span class="overview-value">{version}</span></div>
            <div class="overview-item"><span class="overview-label">最近更新</span><span class="overview-value" style="font-size:14px;line-height:24px;">{updated_at}</span></div>
            <div class="overview-item"><span class="overview-label">材料总数</span><span class="overview-value">{material_count}</span></div>
            <div class="overview-item"><span class="overview-label">流程状态</span><span class="overview-value">{flow_status}</span></div>
            <div class="overview-item"><span class="overview-label">待人工确认</span><span class="overview-value alert">{pending}</span></div>
            <div class="overview-item"><span class="overview-label">整体风险等级</span><span class="tag {risk_class}" style="margin-top:4px;">{risk_label}</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_dimension_cards(state: ProjectState, has_data: bool, run_done: bool) -> None:
    dimensions = _dimension_specs()
    for row_start in range(0, len(dimensions), 2):
        cols = st.columns(2, gap="medium")
        for col, spec in zip(cols, dimensions[row_start : row_start + 2]):
            title = spec["title"]
            key = spec["id"]
            data_key = spec["data_key"]
            fallback = spec["fallback"]
            text = _short_text(state.get(data_key, "") if has_data else fallback, limit=96)
            tag_text = "待接入" if not has_data else ("已审阅" if run_done else "已分流")
            tag_class = "low" if not has_data else ("medium" if run_done else "status")
            metric_rows = _dimension_metrics(key, state)[:2] if has_data else []
            metric_html = "".join(
                "<li>"
                f"<span class='dim-data-label'>{escape(label)}</span>"
                f"<span class='dim-data-value'>{escape(value)}</span>"
                "</li>"
                for label, value, _ in metric_rows
            )
            with col:
                with st.container(border=True):
                    st.markdown(
                        f"""
                        <div class="dim-card-shell">
                            <div class="card-header">
                                <span class="card-title">{escape(title)}</span>
                                <span class="tag {tag_class}">{escape(tag_text)}</span>
                            </div>
                            <div class="dim-summary">{escape(text)}</div>
                            <ul class="dim-data-list">{metric_html}</ul>
                            <div class="dim-slot">负责节点：{escape(spec['agent'])}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    if st.button("查看详情 ->", key=f"open_dim_{key}", disabled=not has_data, use_container_width=True):
                        st.session_state.active_dimension = key
                        st.rerun()


def _agent_patch_count(state: ProjectState, *keys: str) -> int:
    return sum(len(state.get(key, []) or []) for key in keys)


def _chapter_status(
    has_data: bool,
    run_done: bool,
    patch_count: int,
    complete_when_scanned: bool = False,
) -> tuple[str, str, int]:
    if not has_data:
        return "status", "待接入", 0
    if not run_done:
        return "medium", "待扫描", 24
    if patch_count > 0 or complete_when_scanned:
        progress = min(92, 56 + patch_count * 12)
        return "status", "可生成", progress
    return "medium", "待补材料", 38


def _overall_report_chapters(state: ProjectState, has_data: bool, run_done: bool) -> list[dict[str, object]]:
    titles = _chapter_title_map()
    asset_count = _agent_patch_count(state, "asset_patches")
    economic_count = _agent_patch_count(state, "economic_patches")
    legal_count = _agent_patch_count(state, "legal_patches")
    financial_count = _agent_patch_count(state, "financial_patches")
    all_count = asset_count + economic_count + legal_count + financial_count
    has_report = bool(str(state.get("final_report", "") or "").strip())

    core_class, core_label, core_progress = _chapter_status(has_data, run_done, all_count, has_report)
    transaction_count = legal_count + financial_count
    transaction_class, transaction_label, transaction_progress = _chapter_status(has_data, run_done, transaction_count)
    if run_done and transaction_count > 0 and int(state.get("pcs_score", 0) or 0) < 50:
        transaction_class, transaction_label = "high", "关键风险"
    finance_class, finance_label, finance_progress = _chapter_status(has_data, run_done, financial_count)
    if run_done and financial_count == 0:
        finance_class, finance_label = "high", "缺数据"

    return [
        {
            "title": titles["core"],
            "desc": "汇总 PCS 宣判、核心风险、投资建议和下一步动作。",
            "tag_class": core_class,
            "tag": "已生成" if has_report else core_label,
            "progress": 96 if has_report else core_progress,
            "target": "core",
        },
        {
            "title": titles["asset"],
            "desc": "项目概况、资产清单、权属证照、抵押查封和可处置边界。",
            "tag_class": _chapter_status(has_data, run_done, asset_count)[0],
            "tag": _chapter_status(has_data, run_done, asset_count)[1],
            "progress": _chapter_status(has_data, run_done, asset_count)[2],
            "target": "asset",
        },
        {
            "title": titles["market"],
            "desc": "市场价格、货值假设、去化节奏、回款能力和外部对标。",
            "tag_class": _chapter_status(has_data, run_done, economic_count)[0],
            "tag": _chapter_status(has_data, run_done, economic_count)[1],
            "progress": _chapter_status(has_data, run_done, economic_count)[2],
            "target": "market",
        },
        {
            "title": titles["strategy"],
            "desc": "结合资产条件和经济测算，沉淀产品定位、开发节奏和经营策略。",
            "tag_class": "medium" if has_data and not run_done else ("status" if economic_count and asset_count else "medium"),
            "tag": "可生成" if run_done and economic_count and asset_count else ("待扫描" if has_data else "待接入"),
            "progress": 70 if run_done and economic_count and asset_count else (24 if has_data else 0),
            "target": "strategy",
        },
        {
            "title": titles["transaction"],
            "desc": "股权链条、资金路径、债权口径、担保措施和退出安排。",
            "tag_class": transaction_class,
            "tag": transaction_label,
            "progress": transaction_progress,
            "target": "transaction",
        },
        {
            "title": titles["finance"],
            "desc": "债务结构、复工成本、税费、资金缺口、清偿率和敏感性情景。",
            "tag_class": finance_class,
            "tag": finance_label,
            "progress": finance_progress,
            "target": "finance",
        },
    ]


def _render_report_chapter_card(chapter: dict[str, object]) -> None:
    st.markdown(
        f"""
        <div class="report-chapter-card">
            <div>
                <div class="report-chapter-title">{escape(str(chapter["title"]))}</div>
                <div class="report-chapter-desc">{escape(str(chapter["desc"]))}</div>
            </div>
            <div class="chapter-meta">
                <div class="chapter-progress"><span style="width:{int(chapter["progress"])}%;"></span></div>
                <span class="tag {escape(str(chapter["tag_class"]))}">{escape(str(chapter["tag"]))}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_overall_report_panel(state: ProjectState, has_data: bool, run_done: bool) -> None:
    chapters = _overall_report_chapters(state, has_data, run_done)
    completed = sum(1 for item in chapters if item["tag"] in {"可生成", "已生成"})
    missing = sum(1 for item in chapters if item["tag_class"] == "high")
    pending = sum(1 for item in chapters if item["tag"] in {"待扫描", "待补材料", "待接入"})
    completion = round(sum(int(item["progress"]) for item in chapters) / (len(chapters) * 100) * 100)
    report_summary = (
        "已接入四维度 Agent 输出，可生成整体尽调报告章节。"
        if run_done
        else ("项目材料已分流，启动扫描后生成六大报告章节。" if has_data else "上传或载入项目材料后，将自动汇总四维度结论并生成六大报告章节。")
    )

    st.markdown("<div class='workbench-section-title'>整体报告</div>", unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="overall-report-panel">
            <div class="overall-report-head">
                <div class="overall-report-summary">
                    <strong>项目整体尽调报告</strong>
                    <span>{escape(report_summary)}</span>
                </div>
                <div class="report-stat">
                    <span>章节完成度</span>
                    <strong>{completion}%</strong>
                    <em>6 章 / {completed} 章可生成</em>
                </div>
                <div class="report-stat">
                    <span>关键缺口</span>
                    <strong>{missing} 项</strong>
                    <em>红色章节需优先复核</em>
                </div>
                <div class="report-stat">
                    <span>待业务确认</span>
                    <strong>{pending} 项</strong>
                    <em>随材料和扫描状态更新</em>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    for row_start in range(0, len(chapters), 3):
        cols = st.columns(3, gap="small")
        for col, chapter in zip(cols, chapters[row_start : row_start + 3]):
            with col:
                _render_report_chapter_card(chapter)
                target = str(chapter["target"])
                if st.button("查看章节", key=f"report_chapter_{target}_{row_start}", use_container_width=True):
                    st.session_state.active_report_view = True
                    st.session_state.active_report_chapter = target
                    st.rerun()

    action_cols = st.columns([1, 1, 1], gap="small")
    with action_cols[0]:
        if st.button("阅读完整报告", key="open_overall_report", use_container_width=True):
            st.session_state.active_report_view = True
            st.session_state.active_report_chapter = "core"
            st.rerun()
    with action_cols[1]:
        if st.button("查看报告框架", key="open_report_framework", use_container_width=True):
            st.session_state.show_report_framework = not st.session_state.get("show_report_framework", False)
            st.rerun()
    with action_cols[2]:
        if st.session_state.run_dir and run_done:
            report_path = Path(st.session_state.run_dir) / "final_report.md"
            st.download_button(
                "导出报告",
                data=report_path.read_text(encoding="utf-8") if report_path.exists() else str(state.get("final_report", "")),
                file_name="final_report.md",
                mime="text/markdown",
                use_container_width=True,
            )
        else:
            st.button("导出报告", key="export_report_disabled", disabled=True, use_container_width=True)

    if st.session_state.get("show_report_framework", False):
        _render_report_framework(chapters)


def _render_report_framework(chapters: list[dict[str, object]]) -> None:
    rows = "\n".join(
        "<tr>"
        f"<td>{escape(str(item['title']))}</td>"
        f"<td>{escape(str(item['desc']))}</td>"
        f"<td><span class='tag {escape(str(item['tag_class']))}'>{escape(str(item['tag']))}</span></td>"
        "</tr>"
        for item in chapters
    )
    st.markdown(
        f"""
        <div class="card">
            <div class="card-header">
                <span class="card-title">整体报告框架确认</span>
                <span class="tag medium">随 Agent 输出更新</span>
            </div>
            <table>
                <thead><tr><th>章节</th><th>二级内容</th><th>状态</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _patch_rows(title: str, patches: list[str], limit: int = 5) -> str:
    if not patches:
        return (
            "<tr>"
            f"<td>{escape(title)}</td>"
            "<td>等待 Agent 输出。</td>"
            "<td><span class='tag medium'>待生成</span></td>"
            "</tr>"
        )
    rows = []
    for patch in patches[:limit]:
        topic, detail = _split_patch_parts(str(patch))
        level = _risk_level_for_patch(str(patch))
        tag_class = "high" if level == "高风险" else ("medium" if level == "中风险" else "status")
        rows.append(
            "<tr>"
            f"<td>{escape(topic or title)}</td>"
            f"<td>{escape(_short_text(detail or patch, 140))}</td>"
            f"<td><span class='tag {tag_class}'>{escape(level)}</span></td>"
            "</tr>"
        )
    return "".join(rows)


def _report_patch_bucket(state: ProjectState, chapter_key: str) -> list[str]:
    asset = state.get("asset_patches", []) or []
    economic = state.get("economic_patches", []) or []
    legal = state.get("legal_patches", []) or []
    financial = state.get("financial_patches", []) or []
    if chapter_key == "asset":
        return asset
    if chapter_key == "market":
        return economic
    if chapter_key == "strategy":
        return asset[:3] + economic[:4]
    if chapter_key == "transaction":
        return legal + financial[:2]
    if chapter_key == "finance":
        return financial + economic[:2]
    return asset[:2] + economic[:2] + legal[:2] + financial[:2]


def _chapter_title_map() -> dict[str, str]:
    return {
        "core": "一、核心结论与投资建议",
        "asset": "二、资产底盘与权属",
        "market": "三、市场与竞品判断",
        "strategy": "四、产策定位与产品推导",
        "transaction": "五、交易结构与风控",
        "finance": "六、财务测算与敏感性",
    }


def _chapter_focus_map() -> dict[str, str]:
    return {
        "core": "综合四个 Agent 的发现、PCS 分数和 Closer 最终报告，输出投决会可先读的核心结论、主要风险、投资建议和下一步动作。",
        "asset": "围绕资产清单、权属证照、抵押查封、工程状态、可处置边界和资产价值支撑，形成资产底盘专题。",
        "market": "围绕市场价格、货值假设、竞品/可比项目、去化速度、回款节奏和市场风险，形成市场与竞品专题。",
        "strategy": "结合资产条件和经济判断，推导产品定位、开发/复工节奏、经营策略、价格策略和需要补充的产策材料。",
        "transaction": "围绕股权链条、债权顺位、控制权、资金路径、担保措施、司法/查封路径和退出安排，形成交易风控专题。",
        "finance": "围绕债务结构、复工成本、税费、资金缺口、清偿率、收益测算和敏感性情景，形成财务测算专题。",
    }


def _report_generation_fingerprint(state: ProjectState, chapter_key: str) -> str:
    payload = {
        "chapter": chapter_key,
        "asset": state.get("asset_patches", []) or [],
        "economic": state.get("economic_patches", []) or [],
        "legal": state.get("legal_patches", []) or [],
        "financial": state.get("financial_patches", []) or [],
        "pcs_score": state.get("pcs_score", 0),
        "pcs_breakdown": state.get("pcs_breakdown", {}),
        "final_report": state.get("final_report", "") or "",
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _generate_report_chapter_with_llm(state: ProjectState, chapter_key: str) -> str:
    cache_key = _report_generation_fingerprint(state, chapter_key)
    cache = st.session_state.get("report_chapter_cache", {})
    if cache_key in cache:
        return str(cache[cache_key])

    title = _chapter_title_map().get(chapter_key, _chapter_title_map()["core"])
    focus = _chapter_focus_map().get(chapter_key, _chapter_focus_map()["core"])
    system_prompt = f"""
你是困境资产投融资项目的整体尽调报告写作 Agent，正在生成《{title}》章节。
你只能基于用户提供的四个 Agent 输出、PCS 扣分矩阵和 Closer 最终报告写作，不能引入外部事实，不能编造来源。
写作要求：
1. 输出 Markdown。
2. 结构必须包含：章节判断、关键依据、主要风险、待核实事项、投决建议。
3. 所有金额、比例、主体、资产、债权、查封、成本、去化等事实必须来自输入；没有证据就写“待补充材料确认”。
4. 聚焦本章节主题：{focus}
5. 语言要像投决材料，直接、审慎、可执行。
"""
    user_payload = json.dumps(
        {
            "chapter_key": chapter_key,
            "chapter_title": title,
            "chapter_focus": focus,
            "asset_agent_patches": state.get("asset_patches", []) or [],
            "economic_agent_patches": state.get("economic_patches", []) or [],
            "legal_agent_patches": state.get("legal_patches", []) or [],
            "financial_agent_patches": state.get("financial_patches", []) or [],
            "pcs_score": state.get("pcs_score", 0),
            "pcs_breakdown": state.get("pcs_breakdown", {}),
            "closer_final_report": state.get("final_report", "") or "",
        },
        ensure_ascii=False,
    )
    generated = _invoke_brain(system_prompt, user_payload).strip()
    cache[cache_key] = generated
    st.session_state.report_chapter_cache = cache
    return generated


def _pre_generate_report_chapters(state: ProjectState) -> None:
    if not _is_brain_ready():
        return
    chapter_keys = ["core", "asset", "market", "strategy", "transaction", "finance"]
    progress = st.progress(0, text="正在生成整体报告 6 个专题...")
    status = st.empty()
    for index, chapter_key in enumerate(chapter_keys, start=1):
        title = _chapter_title_map().get(chapter_key, chapter_key)
        status.info(f"正在生成整体报告专题：{title} ({index}/6)")
        try:
            _generate_report_chapter_with_llm(state, chapter_key)
        except Exception as exc:
            logger.warning("Pre-generate report chapter failed | chapter=%s | error=%s", chapter_key, exc)
            status.warning(f"{title} 生成失败，用户进入章节时会重试：{exc}")
        progress.progress(index / len(chapter_keys), text=f"整体报告专题生成进度 {index}/6")
    status.success("整体报告 6 个专题已生成。")
    time.sleep(0.6)
    status.empty()
    progress.empty()


def _report_chapter_content(state: ProjectState, chapter_key: str, has_data: bool, run_done: bool) -> dict[str, object]:
    pcs_score = int(state.get("pcs_score", 0) or 0) if run_done else None
    final_report = str(state.get("final_report", "") or "").strip()
    patches = _report_patch_bucket(state, chapter_key)
    title_map = _chapter_title_map()
    lead_map = {
        "core": "本章由四个 Agent 的专题结论、PCS 扣分矩阵和 Closer LLM 最终报告汇总形成，供投决会先读。",
        "asset": "本章聚焦资产清单、权属边界、抵押查封和可处置性，主要来自资产 Agent 输出。",
        "market": "本章聚焦货值、价格假设、去化节奏和回款能力，主要来自经济 Agent 输出。",
        "strategy": "本章把资产条件与经济判断合并，形成产品定位、开发节奏和经营策略的推导链。",
        "transaction": "本章聚焦交易路径、债权顺位、控制权、担保措施和退出安排，主要来自法律与财务 Agent 输出。",
        "finance": "本章聚焦债务、成本、税费、资金缺口、清偿率和敏感性，主要来自财务 Agent 输出。",
    }
    if not has_data:
        summary = "当前尚未接入项目材料，无法生成本章正文。请先上传资料包或载入演示结构化数据。"
    elif not run_done:
        summary = "项目材料已接入，但四 Agent 审阅尚未完成。本章将以当前材料槽位作为草稿基础，扫描后补齐风险判断和正文。"
    elif patches:
        summary = _short_text("；".join(str(item) for item in patches[:3]), 220)
    else:
        summary = "四 Agent 已完成扫描，但本章没有识别出明确专题输出，建议补充材料或复核分流口径。"

    if chapter_key == "core" and final_report:
        summary = _short_text(final_report, 260)

    risk_items = [_short_text(str(item), 120) for item in patches[:5]]
    if not risk_items and final_report:
        risk_items = [_short_text(item, 120) for item in re.split(r"\n+", final_report) if item.strip()][:5]
    if not risk_items:
        risk_items = ["等待 Agent 输出关键发现。"]

    review_items = [_topic_review_item(str(item)) for item in patches[:4]] if patches else []
    if not review_items:
        review_items = ["补充材料后复核证据来源、金额口径和对 PCS 结论的影响。"]

    if pcs_score is None:
        decision = "待 PCS 宣判"
        decision_detail = "四 Agent 扫描完成后生成投决分数。"
    elif pcs_score < 50:
        decision = "红线拦截"
        decision_detail = f"PCS {pcs_score}，需先修复关键风险再进入交易。"
    elif pcs_score < 75:
        decision = "条件推进"
        decision_detail = f"PCS {pcs_score}，可进入复核，但需锁定关键前置条件。"
    else:
        decision = "条件通过"
        decision_detail = f"PCS {pcs_score}，具备进入投决复核基础。"

    return {
        "title": title_map.get(chapter_key, title_map["core"]),
        "lead": lead_map.get(chapter_key, lead_map["core"]),
        "summary": summary,
        "risk_items": risk_items,
        "review_items": review_items,
        "decision": decision,
        "decision_detail": decision_detail,
        "patches": patches,
        "final_report": final_report,
    }


def _render_report_chapter_content(state: ProjectState, chapter_key: str, has_data: bool, run_done: bool) -> None:
    content = _report_chapter_content(state, chapter_key, has_data, run_done)
    llm_body = ""
    llm_error = ""
    if run_done and _is_brain_ready():
        cols = st.columns([1, 5], gap="small")
        with cols[0]:
            if st.button("重新生成本章", key=f"regen_report_chapter_{chapter_key}", use_container_width=True):
                cache_key = _report_generation_fingerprint(state, chapter_key)
                st.session_state.get("report_chapter_cache", {}).pop(cache_key, None)
                st.rerun()
        cache_key = _report_generation_fingerprint(state, chapter_key)
        cache = st.session_state.get("report_chapter_cache", {})
        context = st.spinner(f"LLM 正在生成《{content['title']}》专题正文...") if cache_key not in cache else nullcontext()
        with context:
            try:
                llm_body = _generate_report_chapter_with_llm(state, chapter_key)
            except Exception as exc:
                logger.warning("Report chapter LLM generation failed | chapter=%s | error=%s", chapter_key, exc)
                llm_error = str(exc)
    st.markdown(
        f"""
        <div class="generated-report-body">
            <div class="report-section-title">{escape(str(content["title"]))}</div>
            <div class="report-chapter-desc">{escape(str(content["lead"]))}</div>
            <div class="report-insight-grid">
                <div class="report-insight-card"><strong>章节摘要</strong><span>{escape(str(content["summary"]))}</span></div>
                <div class="report-insight-card"><strong>投决状态</strong><span>{escape(str(content["decision"]))}<br>{escape(str(content["decision_detail"]))}</span></div>
                <div class="report-insight-card"><strong>生成依据</strong><span>四 Agent 专题输出、PCS 扣分矩阵、Closer LLM 最终报告。</span></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if llm_body:
        st.markdown("##### LLM 生成专题正文")
        st.markdown(llm_body)
    elif run_done and not _is_brain_ready():
        st.warning("当前未配置 BRAIN_API_KEY 或 OPENAI_API_KEY，无法调用 LLM 生成专题正文；下方展示 Agent 结果兜底视图。")
    elif llm_error:
        st.warning(f"LLM 生成失败，已展示 Agent 结果兜底视图：{llm_error}")
    else:
        st.markdown("##### 关键发现")
        st.markdown(
            "<ul class='report-content-list'>"
            + "".join(f"<li>{escape(str(item))}</li>" for item in content["risk_items"])
            + "</ul>",
            unsafe_allow_html=True,
        )
    st.markdown("##### Agent 证据与风险分层")
    st.markdown(
        "<table class='report-mini-table'><thead><tr><th>专题</th><th>Agent 结论摘录</th><th>风险等级</th></tr></thead>"
        f"<tbody>{_patch_rows(str(content['title']), list(content['patches']))}</tbody></table>",
        unsafe_allow_html=True,
    )
    st.markdown("##### 待确认事项")
    st.markdown(
        "<ul class='report-content-list'>"
        + "".join(f"<li>{escape(str(item))}</li>" for item in content["review_items"])
        + "</ul>",
        unsafe_allow_html=True,
    )
    if chapter_key == "core" and content["final_report"]:
        st.markdown("##### Closer LLM 最终报告")
        st.markdown(str(content["final_report"]))


def _render_overall_report_detail(state: ProjectState, has_data: bool, run_done: bool) -> None:
    chapters = _overall_report_chapters(state, has_data, run_done)
    selected_target = st.session_state.get("active_report_chapter", "") or "report"

    if st.button("<- 返回总控台", key="back_from_overall_report"):
        st.session_state.active_report_view = False
        st.session_state.active_report_chapter = ""
        st.session_state.show_report_framework = False
        st.rerun()

    st.markdown("<div class='breadcrumb'>总控台 &gt; 整体报告</div>", unsafe_allow_html=True)
    st.markdown(
        """
        <div class="report-detail-card">
            <div class="report-detail-header">
                <div class="report-detail-title">项目整体尽调报告</div>
            </div>
            <div class="report-section-title">章节生成与确认</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    action_cols = st.columns([1, 1, 4], gap="small")
    with action_cols[0]:
        if st.button("报告框架", key="detail_report_framework", use_container_width=True):
            st.session_state.show_report_framework = not st.session_state.get("show_report_framework", False)
            st.rerun()
    with action_cols[1]:
        if st.session_state.run_dir and run_done:
            report_path = Path(st.session_state.run_dir) / "final_report.md"
            st.download_button(
                "导出报告",
                data=report_path.read_text(encoding="utf-8") if report_path.exists() else str(state.get("final_report", "")),
                file_name="final_report.md",
                mime="text/markdown",
                use_container_width=True,
            )
        else:
            st.button("导出报告", key="detail_export_report_disabled", disabled=True, use_container_width=True)

    for row_start in range(0, len(chapters), 3):
        cols = st.columns(3, gap="small")
        for index, (col, chapter) in enumerate(zip(cols, chapters[row_start : row_start + 3])):
            target = str(chapter["target"])
            with col:
                _render_report_chapter_card(chapter)
                label = "查看正文" if run_done else "查看草稿"
                if st.button(label, key=f"detail_report_chapter_{row_start}_{index}_{target}", use_container_width=True):
                    st.session_state.active_report_chapter = target
                    st.rerun()

    if st.session_state.get("show_report_framework", False):
        _render_report_framework(chapters)

    valid_targets = {str(item["target"]) for item in chapters}
    if selected_target not in valid_targets:
        selected_target = "core"
    _render_report_chapter_content(state, selected_target, has_data, run_done)


def _dimension_specs() -> list[dict[str, str]]:
    return [
        {
            "id": "asset",
            "title": "资产",
            "data_key": "asset_and_mortgage_data",
            "agent": "资产 Agent",
            "fallback": "资产清单、权属、抵押物、查封受限、可处置价值。",
            "one": "资产：有什么、权属是否清楚、能不能处置。",
            "judgement": "优先判断资产范围、权属证照、抵押查封和可处置价值是否支撑交易。",
        },
        {
            "id": "economic",
            "title": "经济",
            "data_key": "financial_and_cost_data",
            "agent": "经济 Agent",
            "fallback": "市场、货值、价格假设、去化、回款、敏感性。",
            "one": "经济：值多少钱、卖不卖得动、价格假设是否可靠。",
            "judgement": "重点复核货值、价格假设、市场对标、去化和回款节奏是否过于乐观。",
        },
        {
            "id": "legal",
            "title": "法律",
            "data_key": "creditor_and_seizure_data",
            "agent": "法律 Agent",
            "fallback": "主体股权、交易结构、合同、债权债务法律关系、查封执行。",
            "one": "法律：主体、合同、债权和执行路径是否有效。",
            "judgement": "优先确认交易结构、合同安排、债权顺位、查封执行和权利冲突。",
        },
        {
            "id": "financial",
            "title": "财务",
            "data_key": "financial_and_cost_data",
            "agent": "财务 Agent",
            "fallback": "债务结构、成本费用、税费、资金缺口、清偿率、收益测算。",
            "one": "财务：债务多少、还要投多少、清偿和收益是否成立。",
            "judgement": "重点判断债务结构、成本费用、税费测算、资金缺口和清偿率。",
        },
    ]


def _dimension_spec_by_key(key: str) -> dict[str, str]:
    for spec in _dimension_specs():
        if spec["id"] == key:
            return spec
    return _dimension_specs()[0]


def _topics_for_dimension(key: str, state: ProjectState) -> list[str]:
    asset = state.get("asset_patches", []) or []
    economic = state.get("economic_patches", []) or []
    legal = state.get("legal_patches", []) or []
    financial = state.get("financial_patches", []) or []
    if key == "asset":
        patches = asset
    elif key == "economic":
        patches = economic
    elif key == "legal":
        patches = legal
    else:
        patches = financial
    if patches:
        return [str(item) for item in patches[:6]]
    defaults = {
        "asset": ["资产清单", "权属情况", "抵押/质押/查封情况", "资产瑕疵与处置障碍"],
        "economic": ["货值总览", "平均单价与价格假设", "销售去化情况", "回款预测"],
        "legal": ["主体与股权结构", "交易结构与合同安排", "债权债务法律关系", "诉讼/仲裁/执行"],
        "financial": ["债务结构", "成本费用", "现金流与回款", "税费测算", "清偿测算"],
    }
    return defaults.get(key, [])


def _infer_project_name(state: ProjectState) -> str:
    text = _combined_state_text(
        state,
        "equity_and_history_data",
        "asset_and_mortgage_data",
        "creditor_and_seizure_data",
        "financial_and_cost_data",
    )
    patterns = [
        r"项目名称[：:\s]*([^。\n；;，,]{3,50})",
        r"项目公司[：:\s]*([^。\n；;，,]{3,50})",
        r"目标公司[：:\s]*([^。\n；;，,]{3,50})",
        r"债务人[：:\s]*([^。\n；;，,]{3,50})",
        r"([^。\n；;，,]{2,40}(?:项目|资产包|重组|并购|仲裁|诉讼))",
    ]
    value = _first_match(text, patterns, "待识别")
    return value.strip(" -，,。；;") if value != "待识别" else value


def _infer_property_type(state: ProjectState) -> str:
    text = _combined_state_text(state, "asset_and_mortgage_data", "financial_and_cost_data")
    candidates = [
        ("商业/办公综合体", ["商业", "办公"]),
        ("商业/住宅综合体", ["商业", "住宅"]),
        ("文旅综合体", ["文旅"]),
        ("产业园/工业资产", ["产业园", "厂房", "工业"]),
        ("住宅资产", ["住宅"]),
        ("商业资产", ["商业", "商铺"]),
        ("土地资产", ["土地", "宗地"]),
        ("债权资产包", ["债权", "本金", "债务人"]),
    ]
    for label, keywords in candidates:
        if any(keyword in text for keyword in keywords):
            return label
    return "待识别"


def _infer_project_scale(state: ProjectState, material_count: int) -> str:
    text = _combined_state_text(
        state,
        "asset_and_mortgage_data",
        "financial_and_cost_data",
        "creditor_and_seizure_data",
    )
    amount = _first_match(text, [r"(\d+(?:\.\d+)?)\s*亿元", r"(\d+(?:\.\d+)?)\s*亿"], "")
    area = _first_match(text, [r"(\d+(?:\.\d+)?)\s*万平方米", r"(\d+(?:\.\d+)?)\s*万㎡", r"(\d+(?:\.\d+)?)\s*亩"], "")
    try:
        amount_value = float(amount) if amount else 0
    except ValueError:
        amount_value = 0
    try:
        area_value = float(area) if area else 0
    except ValueError:
        area_value = 0
    if amount_value >= 10 or area_value >= 10 or material_count >= 50:
        return "中大型资产"
    if amount_value > 0 or area_value > 0 or material_count > 0:
        return "中小型资产"
    return "待识别"


def _infer_project_stage(state: ProjectState) -> str:
    text = _combined_state_text(
        state,
        "equity_and_history_data",
        "asset_and_mortgage_data",
        "creditor_and_seizure_data",
        "financial_and_cost_data",
    )
    if any(keyword in text for keyword in ["仲裁", "诉讼", "查封", "冻结", "执行"]):
        return "司法处置 / 交易尽调"
    if any(keyword in text for keyword in ["重组", "债务重组", "并购", "收购"]):
        return "重组并购 / 交易结构设计"
    if any(keyword in text for keyword in ["复工", "在建", "停工"]):
        return "投前尽调 / 复工测算"
    return "投前尽调 / 风险识别"


def _project_profile_items(state: ProjectState, has_data: bool, material_count: int) -> list[tuple[str, str, str]]:
    if not has_data:
        return [
            ("项目名称", "待建档", "请先接入项目材料"),
            ("物业类型", "待识别", "由资产清单和权属资料确定"),
            ("项目体量", "待识别", "由规划、面积、货值和债权规模确定"),
            ("项目阶段", "材料接入前", "等待结构化分流"),
        ]
    return [
        ("项目名称", _infer_project_name(state), "从项目名称、项目公司、债务人或材料标题中动态识别"),
        ("物业类型", _infer_property_type(state), "由资产清单、抵押物描述和财务测算口径动态识别"),
        ("项目体量", _infer_project_scale(state, material_count), f"已接入 {material_count} 份材料，结合金额、面积和文件规模判断"),
        ("项目阶段", _infer_project_stage(state), "由诉讼、查封、重组、复工、交易等过程信息判断"),
    ]


def _render_project_definition(state: ProjectState, has_data: bool, material_count: int) -> None:
    rows = "".join(
        "<li>"
        f"<span class='project-definition-label'>{escape(label)}</span>"
        f"<span class='project-definition-value'>{escape(value)}</span>"
        f"<span class='project-definition-desc'>{escape(desc)}</span>"
        "</li>"
        for label, value, desc in _project_profile_items(state, has_data, material_count)
    )
    st.markdown(
        f"""
        <section class="project-definition-card">
            <div class="card-header">
                <span class="card-title">项目定义</span>
                <span class="tag status">Project Profile</span>
            </div>
            <ul class="project-definition-list">{rows}</ul>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _dimension_metrics(key: str, state: ProjectState) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    metric_results = dict(state.get("metric_results") or {})
    for metric_key in DIMENSION_METRIC_KEYS.get(key, []):
        result = metric_results.get(metric_key)
        if not result:
            result = _metric_result(metric_key, state)
            metric_results[metric_key] = result
            state["metric_results"] = metric_results
        rows.append(
            (
                str(result.get("label") or METRIC_CONFIGS[metric_key]["label"]),
                _metric_display_value(result),
                _metric_desc(result),
            )
        )
    return rows


def _render_dimension_detail(state: ProjectState) -> None:
    key = st.session_state.active_dimension
    spec = _dimension_spec_by_key(key)
    if st.button("<- 返回总控台"):
        st.session_state.active_dimension = ""
        st.rerun()

    st.caption(f"总控台 > {spec['title']}")
    st.markdown(f"<div class='dimension-title'>{escape(spec['title'])}维度</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='dimension-one-liner'>{escape(spec['one'])}</div>", unsafe_allow_html=True)

    score = int(state.get("pcs_score", 0)) if st.session_state.run_done else None
    data_key = spec["data_key"]
    _, risk_label = _risk_class(score, bool(state.get(data_key)))
    hero_cols = st.columns([1.45, 1, 1, 1, 1])
    with hero_cols[0]:
        with st.container(border=True):
            st.caption("顶部：维度总判断")
            st.markdown(f"<div class='judgement-title'>{escape(spec['judgement'])}</div>", unsafe_allow_html=True)
            st.markdown(
                f"<div class='detail-source-text'>{escape(_short_text(state.get(data_key, '暂无结构化材料。'), 160))}</div>",
                unsafe_allow_html=True,
            )
            st.caption(f"{risk_label} | 新材料影响：{len(_topics_for_dimension(key, state))} 个专题 | 待确认：{'0' if st.session_state.run_done else '待扫描'} 项")
    for col, (label, value, desc) in zip(hero_cols[1:], _dimension_metrics(key, state)):
        with col:
            st.markdown(
                f"""
                <div class="detail-metric-card">
                    <div class="detail-metric-label">{escape(label)}</div>
                    <div class="detail-metric-value">{escape(value)}</div>
                    <div class="detail-metric-desc">{escape(desc)}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("### 专题模块")
    topics = _topics_for_dimension(key, state)
    for row_start in range(0, len(topics), 2):
        cols = st.columns(2)
        for col, topic in zip(cols, topics[row_start : row_start + 2]):
            topic_title, detail = _split_patch_parts(topic)
            level = _risk_level_for_patch(topic)
            with col:
                with st.container(border=True):
                    st.markdown(f"<div class='topic-title'>{escape(topic_title)}</div>", unsafe_allow_html=True)
                    if level == "高风险":
                        st.error(level)
                    elif level == "中风险":
                        st.warning(level)
                    else:
                        st.info(level)
                    st.markdown(f"<div class='topic-body'>{escape(detail or topic)}</div>", unsafe_allow_html=True)
                    st.markdown(
                        f"""
                        <div class="topic-meta">
                            <div class="topic-meta-line"><span class="topic-meta-label">关键指标：</span>{escape(_topic_key_metric(topic))}</div>
                            <div class="topic-meta-line"><span class="topic-meta-label">待核实事项：</span>{escape(_topic_review_item(topic))}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    st.progress(0.85 if level == "高风险" else 0.62 if level == "中风险" else 0.42)

    evidence_cols = st.columns(3)
    with evidence_cols[0]:
        with st.container(border=True):
            st.markdown("#### 关联源文件")
            st.markdown("<div class='detail-source-text'>- 项目资料包<br>- 结构化文本<br>- Agent 专题输出</div>", unsafe_allow_html=True)
    with evidence_cols[1]:
        with st.container(border=True):
            st.markdown("#### 待追问问题")
            st.markdown(
                "<div class='detail-source-text'>- 是否存在材料缺口或口径冲突？<br>- 是否需要补充权属、债权或成本证明？<br>- 是否改变 PCS 投决结论？</div>",
                unsafe_allow_html=True,
            )
    with evidence_cols[2]:
        with st.container(border=True):
            st.markdown("#### 更新记录")
            st.markdown(
                f"<div class='detail-source-text'>- 最近更新：{escape(st.session_state.get('project_updated_at') or '--')}<br>- 材料总数：{st.session_state.get('material_count', 0)}<br>- 状态：已进入维度详情复核</div>",
                unsafe_allow_html=True,
            )


def _render_process_card(has_data: bool, run_done: bool, score: int | None) -> None:
    parse_status = "完成" if has_data else "等待"
    agent_status = "完成" if run_done else ("待启动" if has_data else "等待")
    verdict_status = f"PCS {score}" if run_done and score is not None else "等待"
    with st.container(border=True):
        st.markdown("#### 流程轨迹")
        st.caption("LangGraph")
        st.markdown(f"**1. 材料接入**  \n上传 CSV / PDF / Word / Excel，或读取本地资料包。状态：{parse_status}")
        st.markdown("**2. 四维分流**  \n结构化为项目基础、经济财务、法律债权、资产抵押四个输入槽。")
        st.markdown(f"**3. 四 Agent 审阅**  \n资产、经济、法律、财务 Agent 并发生成专题板块。状态：{agent_status}")
        st.markdown(f"**4. PCS 宣判**  \n总控节点执行扣分矩阵并生成投决报告。状态：{verdict_status}")


def _render_pcs_breakdown(breakdown: dict) -> None:
    if not breakdown:
        return
    st.caption("PCS 扣分明细：四维度封顶扣分，避免重复风险直接归零。")
    for key in ("asset", "economic", "legal", "financial"):
        item = breakdown.get(key, {})
        if not item:
            continue
        label = item.get("label", key)
        deduction = int(item.get("deduction", 0))
        cap = int(item.get("cap", 0))
        matched = item.get("matched", [])
        st.progress((cap - deduction) / cap if cap else 0)
        keywords = "、".join(str(hit.get("keyword")) for hit in matched) if matched else "未命中关键扣分项"
        st.caption(f"{label}：扣 {deduction}/{cap} 分；命中：{keywords}")


def _inject_sample_data() -> None:
    state = empty_state()
    state.update(SAMPLE_DATA)
    state["csv_evidence_rows"] = _state_text_evidence_rows(state)
    state["metric_results"] = {}
    st.session_state.project_state = state
    st.session_state.metric_result_cache = {}
    st.session_state.report_chapter_cache = {}
    st.session_state.run_done = False
    st.session_state.event_log = ["演示样本已载入：海南陵水项目四类结构化文本已分流。"]
    st.session_state.ocr_markdown = ""
    st.session_state.run_dir = ""
    st.session_state.material_count = len(SAMPLE_DATA)
    st.session_state.project_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")


def _save_uploaded_file(uploaded_file) -> Path:
    upload_dir = Path(".tmp_uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(uploaded_file.name).name
    file_path = upload_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
    file_path.write_bytes(uploaded_file.getbuffer())
    logger.info("Uploaded file saved | file=%s | bytes=%s", file_path.name, file_path.stat().st_size)
    return file_path


def _save_uploaded_files(uploaded_files: Iterable) -> list[Path]:
    return [_save_uploaded_file(uploaded_file) for uploaded_file in uploaded_files]


def _collect_folder_files(folder_path: str) -> list[Path]:
    root = Path(folder_path).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"文件夹不存在：{root}")
    if not root.is_dir():
        raise NotADirectoryError(f"不是文件夹路径：{root}")

    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_INPUT_EXTENSIONS
    ]
    if not files:
        raise FileNotFoundError("该文件夹内没有可解析文件。支持 pdf/png/jpg/jpeg/txt/md/docx/xlsx。")
    deduped: dict[tuple[str, int], Path] = {}
    skipped = 0
    for path in sorted(files):
        key = (path.name.lower(), path.stat().st_size)
        if key in deduped:
            skipped += 1
            continue
        deduped[key] = path
    logger.info("Folder files collected | folder=%s | files=%s | deduped=%s | skipped_duplicates=%s", root, len(files), len(deduped), skipped)
    return sorted(deduped.values())


def _create_run_dir() -> Path:
    run_dir = Path("output") / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _save_structured_artifacts(run_dir: Path, ocr_markdown: str, state: ProjectState) -> None:
    (run_dir / "ocr_raw.md").write_text(ocr_markdown, encoding="utf-8")
    (run_dir / "structured_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Structured artifacts saved | dir=%s", run_dir)


def _save_final_report(run_dir: Path, state: ProjectState) -> None:
    if not run_dir:
        return
    report = state.get("final_report", "")
    (run_dir / "final_report.md").write_text(report, encoding="utf-8")
    logger.info("Final report saved | dir=%s", run_dir)


CACHE_DIR = Path("output") / "cache"


def _file_signature(file_path: Path) -> dict:
    stat = file_path.stat()
    return {
        "path": str(file_path.resolve()),
        "name": file_path.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _package_cache_key(file_paths: list[Path], fast_mode: bool, max_ocr_files: int) -> str:
    payload = {
        "version": 2,
        "fast_mode": fast_mode,
        "max_ocr_files": max_ocr_files,
        "files": sorted((_file_signature(path) for path in file_paths), key=lambda item: item["path"]),
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _package_cache_path(cache_key: str) -> Path:
    return CACHE_DIR / "packages" / f"{cache_key}.json"


def _load_package_cache(cache_key: str) -> tuple[ProjectState, str, list[str]] | None:
    cache_path = _package_cache_path(cache_key)
    if not cache_path.exists():
        return None
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    logger.info("Package parse cache hit | key=%s | path=%s", cache_key, cache_path)
    logs = list(payload.get("logs") or [])
    logs.append("命中项目资料包缓存：已跳过 OCR 和结构化分流。")
    return payload.get("state") or empty_state(), str(payload.get("ocr_markdown") or ""), logs


def _save_package_cache(cache_key: str, state: ProjectState, ocr_markdown: str, logs: list[str]) -> None:
    cache_path = _package_cache_path(cache_key)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "state": state,
                "ocr_markdown": ocr_markdown,
                "logs": logs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("Package parse cache saved | key=%s | path=%s", cache_key, cache_path)


def _file_cache_key(file_path: Path) -> str:
    signature = _file_signature(file_path)
    signature["version"] = 2
    return hashlib.sha1(json.dumps(signature, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _file_cache_path(file_path: Path) -> Path:
    return CACHE_DIR / "files" / f"{_file_cache_key(file_path)}.json"


def _load_file_text_cache(file_path: Path) -> tuple[str, str] | None:
    cache_path = _file_cache_path(file_path)
    if not cache_path.exists():
        return None
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    logger.info("File text cache hit | file=%s", file_path.name)
    return str(payload.get("text") or ""), str(payload.get("source") or "cache")


def _save_file_text_cache(file_path: Path, text: str, source: str) -> None:
    if not text.strip():
        return
    cache_path = _file_cache_path(file_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "file": _file_signature(file_path),
                "source": source,
                "text": text,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _read_docx_text(file_path: Path) -> str:
    document = Document(str(file_path))
    parts: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            parts.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            line = " | ".join(cell for cell in cells if cell)
            if line:
                parts.append(line)
    return "\n".join(parts).strip()


def _read_xlsx_text(file_path: Path, max_rows_per_sheet: int = 300) -> str:
    workbook = load_workbook(file_path, read_only=True, data_only=True)
    chunks: list[str] = []
    for sheet in workbook.worksheets:
        chunks.append(f"### Sheet: {sheet.title}")
        for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            if row_index > max_rows_per_sheet:
                chunks.append(f"... 已截断，超过 {max_rows_per_sheet} 行")
                break
            values = [str(value).strip() for value in row if value is not None and str(value).strip()]
            if values:
                chunks.append(" | ".join(values))
    workbook.close()
    return "\n".join(chunks).strip()


def _read_csv_rows(file_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    raw = file_path.read_bytes()
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            reader = csv.DictReader(text.splitlines())
            fieldnames = [field.strip() for field in (reader.fieldnames or []) if field and field.strip()]
            rows: list[dict[str, str]] = []
            for row in reader:
                normalized_row = {
                    (key or "").strip(): (value or "").strip()
                    for key, value in row.items()
                    if key and str(value or "").strip()
                }
                if normalized_row:
                    rows.append(normalized_row)
            return fieldnames, rows
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"CSV 编码无法识别：{file_path.name}") from last_error


def _classify_csv_file(file_path: Path) -> str | None:
    name = file_path.name.lower()
    for category, config in CSV_CATEGORY_CONFIG.items():
        if any(keyword.lower() in name for keyword in config["keywords"]):
            return category
    return None


def _select_csv_files(file_paths: list[Path]) -> tuple[dict[str, Path], list[str]]:
    selected: dict[str, Path] = {}
    logs: list[str] = []
    for file_path in sorted(file_paths, key=lambda path: ("_all" not in path.stem.lower(), path.name)):
        if file_path.suffix.lower() not in CSV_EXTENSIONS:
            continue
        category = _classify_csv_file(file_path)
        if not category:
            logs.append(f"CSV 未识别分类，已跳过：{file_path.name}")
            continue
        if category in selected:
            logs.append(f"{CSV_CATEGORY_CONFIG[category]['label']} 已有文件，跳过重复 CSV：{file_path.name}")
            continue
        selected[category] = file_path
        logs.append(f"{CSV_CATEGORY_CONFIG[category]['label']} 已识别：{file_path.name}")
    return selected, logs


def _csv_rows_to_markdown(category: str, file_path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> str:
    label = CSV_CATEGORY_CONFIG[category]["label"]
    lines = [
        f"## {label}",
        f"- 来源文件：{file_path.name}",
        f"- 字段：{', '.join(fieldnames) if fieldnames else '未识别表头'}",
        f"- 记录数：{len(rows)}",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        parts = [f"{key}: {value}" for key, value in row.items()]
        lines.append(f"{index}. " + "；".join(parts))
    return "\n".join(lines).strip()


def _csv_rows_to_evidence_rows(category: str, file_path: Path, rows: list[dict[str, str]]) -> list[dict]:
    evidence_rows: list[dict] = []
    sheet = CSV_CATEGORY_CONFIG[category]["label"]
    for row_index, row in enumerate(rows, start=1):
        row_text = "；".join(f"{key}: {value}" for key, value in row.items() if str(value or "").strip())
        date_version = ""
        for date_key in ("日期", "版本", "date", "version", "更新时间"):
            if row.get(date_key):
                date_version = str(row.get(date_key) or "")
                break
        if row_text:
            evidence_rows.append(
                {
                    "file": file_path.name,
                    "sheet": sheet,
                    "row_index": row_index,
                    "column": "__row__",
                    "text": row_text,
                    "category": category,
                    "date_version": date_version,
                }
            )
        for column, value in row.items():
            text = str(value or "").strip()
            if not text:
                continue
            evidence_rows.append(
                {
                    "file": file_path.name,
                    "sheet": sheet,
                    "row_index": row_index,
                    "column": str(column),
                    "text": text,
                    "category": category,
                    "date_version": date_version,
                }
            )
    return evidence_rows


def _parse_csv_files_to_state(file_paths: list[Path]) -> tuple[ProjectState, str, list[str]]:
    cache_key = _package_cache_key(file_paths, fast_mode=False, max_ocr_files=0)
    cached = _load_package_cache(cache_key)
    if cached:
        return cached

    selected, logs = _select_csv_files(file_paths)
    state = empty_state()
    markdown_chunks: list[str] = []
    evidence_rows: list[dict] = []
    for category, config in CSV_CATEGORY_CONFIG.items():
        file_path = selected.get(category)
        if not file_path:
            missing = f"未上传或未识别到{config['label']}。"
            state[config["state_key"]] = missing
            logs.append(missing)
            continue
        fieldnames, rows = _read_csv_rows(file_path)
        markdown = _csv_rows_to_markdown(category, file_path, fieldnames, rows)
        state[config["state_key"]] = markdown
        evidence_rows.extend(_csv_rows_to_evidence_rows(category, file_path, rows))
        markdown_chunks.append(markdown)
        logs.append(f"{config['label']} 解析完成：{len(rows)} 条记录")
    state["csv_evidence_rows"] = evidence_rows
    merged_markdown = "\n\n---\n\n".join(markdown_chunks).strip()
    if not merged_markdown:
        raise ValueError("未识别到可解析的四类 CSV 文件。")
    logger.info("CSV package parsing finished | csv_categories=%s", ",".join(selected.keys()))
    _save_package_cache(cache_key, state, merged_markdown, logs)
    return state, merged_markdown, logs


def _can_parse_without_brain(file_paths: list[Path]) -> bool:
    csv_files = [path for path in file_paths if path.suffix.lower() in CSV_EXTENSIONS]
    return bool(csv_files) and len(csv_files) == len(file_paths)


def _rank_files_for_fast_mode(file_paths: list[Path]) -> list[Path]:
    keywords = [
        "交易",
        "重组",
        "债权",
        "查封",
        "抵押",
        "评估",
        "测算",
        "财务",
        "融资",
        "项目",
        "股权",
        "成本",
        "资产",
        "清册",
    ]

    def score(path: Path) -> tuple[int, str]:
        name = path.name
        keyword_score = sum(1 for keyword in keywords if keyword in name)
        type_score = 2 if path.suffix.lower() in OFFICE_EXTENSIONS | TEXT_EXTENSIONS else 0
        return (keyword_score + type_score, name)

    return sorted(file_paths, key=score, reverse=True)


def _parse_files_to_state(
    file_paths: list[Path],
    *,
    fast_mode: bool = True,
    max_ocr_files: int = 20,
) -> tuple[ProjectState, str, list[str]]:
    cache_key = _package_cache_key(file_paths, fast_mode, max_ocr_files)
    cached = _load_package_cache(cache_key)
    if cached:
        return cached

    markdown_chunks: list[str] = []
    logs: list[str] = []
    ocr_count = 0
    ordered_files = _rank_files_for_fast_mode(file_paths) if fast_mode else file_paths
    logger.info(
        "Asset package parsing started | files=%s | fast_mode=%s | max_ocr_files=%s",
        len(file_paths),
        fast_mode,
        max_ocr_files,
    )

    for index, file_path in enumerate(ordered_files, start=1):
        suffix = file_path.suffix.lower()
        title = f"## 文件 {index}: {file_path.name}"
        logger.info("Parsing file | index=%s | file=%s | suffix=%s", index, file_path.name, suffix)
        try:
            cached_text = _load_file_text_cache(file_path)
            if cached_text:
                text, source = cached_text
                if text:
                    markdown_chunks.append(f"{title}\n\n{text}")
                    logs.append(f"命中文件缓存：{file_path.name} ({source})")
                continue

            if suffix in TEXT_EXTENSIONS:
                text = file_path.read_text(encoding="utf-8", errors="ignore").strip()
                if text:
                    _save_file_text_cache(file_path, text, "text")
                    markdown_chunks.append(f"{title}\n\n{text}")
                    logs.append(f"直接读取文本：{file_path.name}")
                continue

            if suffix == ".docx":
                text = _read_docx_text(file_path)
                if text:
                    _save_file_text_cache(file_path, text, "docx")
                    markdown_chunks.append(f"{title}\n\n{text}")
                    logs.append(f"DOCX 直接抽文本：{file_path.name}")
                continue

            if suffix == ".xlsx":
                text = _read_xlsx_text(file_path)
                if text:
                    _save_file_text_cache(file_path, text, "xlsx")
                    markdown_chunks.append(f"{title}\n\n{text}")
                    logs.append(f"XLSX 直接抽表格：{file_path.name}")
                continue

            if suffix in OCR_EXTENSIONS:
                if fast_mode and ocr_count >= max_ocr_files:
                    logs.append(f"快速模式跳过 OCR：{file_path.name}")
                    logger.info("Fast mode skipped OCR file | file=%s", file_path.name)
                    continue
                ocr_count += 1
                ocr_result = run_paddleocr(file_path)
                _save_file_text_cache(file_path, ocr_result.markdown, f"ocr:{ocr_result.job_id}")
                markdown_chunks.append(f"{title}\n\n{ocr_result.markdown}")
                logs.append(f"PaddleOCR 完成：{file_path.name}, job_id={ocr_result.job_id}, pages={ocr_result.pages}")
                continue

            logs.append(f"跳过不支持类型：{file_path.name}")
        except Exception as exc:
            logs.append(f"文件解析失败，已跳过：{file_path.name} ({exc})")
            logger.exception("File parsing failed and skipped | file=%s", file_path)
            continue

    merged_markdown = "\n\n---\n\n".join(markdown_chunks).strip()
    if not merged_markdown:
        raise ValueError("未获得可用于结构化分流的文本。")

    structured_state = structure_markdown_to_state(merged_markdown)
    logger.info("Asset package parsing finished | files=%s | merged_chars=%s", len(file_paths), len(merged_markdown))
    _save_package_cache(cache_key, structured_state, merged_markdown, logs)
    return structured_state, merged_markdown, logs


def _stream_graph_run(initial_state: ProjectState) -> ProjectState:
    asset_box = st.empty()
    economic_box = st.empty()
    legal_box = st.empty()
    financial_box = st.empty()
    closer_box = st.empty()
    run_state: ProjectState = {
        **initial_state,
        "asset_patches": [],
        "economic_patches": [],
        "legal_patches": [],
        "financial_patches": [],
        "pcs_score": 0,
        "pcs_breakdown": {},
        "final_report": "",
    }
    final_state: ProjectState = dict(run_state)

    with st.spinner("LangGraph 正在并发调度资产、经济、法律、财务四个 Agent..."):
        graph_config = {
            "run_name": "distressed_asset_xray_graph",
            "metadata": {
                "app": "困境资产X光机",
                "project": os.getenv("LANGSMITH_PROJECT") or os.getenv("LANGCHAIN_PROJECT"),
            },
        }
        for event in compiled_graph.stream(run_state, stream_mode="updates", config=graph_config):
            st.session_state.event_log.append(str(event))
            if "asset_agent_node" in event:
                patches = event["asset_agent_node"].get("asset_patches", [])
                final_state["asset_patches"] = final_state.get("asset_patches", []) + patches
                asset_box.markdown(
                    "#### 资产 Agent Patches\n"
                    + "\n".join(f"- {item}" for item in patches),
                )
            if "economic_agent_node" in event:
                patches = event["economic_agent_node"].get("economic_patches", [])
                final_state["economic_patches"] = final_state.get("economic_patches", []) + patches
                economic_box.markdown(
                    "#### 经济 Agent Patches\n"
                    + "\n".join(f"- {item}" for item in patches),
                )
            if "legal_agent_node" in event:
                patches = event["legal_agent_node"].get("legal_patches", [])
                final_state["legal_patches"] = final_state.get("legal_patches", []) + patches
                legal_box.markdown(
                    "#### 法律 Agent Patches\n"
                    + "\n".join(f"- {item}" for item in patches),
                )
            if "financial_node" in event:
                patches = event["financial_node"].get("financial_patches", [])
                final_state["financial_patches"] = final_state.get("financial_patches", []) + patches
                financial_box.markdown(
                    "#### 财务 Agent Patches\n"
                    + "\n".join(f"- {item}" for item in patches),
                )
            if "closer_node" in event:
                final_state.update(event["closer_node"])
                closer_box.info("总控节点已完成 PCS 扣分矩阵与因果推断。")
            time.sleep(0.15)

    return final_state


def main() -> None:
    st.set_page_config(
        page_title="困境资产X光机",
        page_icon="X",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _init_session()
    _render_css()
    _render_workbench_css()

    eyes_status, brain_status, smith_status = _api_status()
    _render_top_header("困境资产 X 光机 | 项目尽调报告自动化生成与阅读系统")

    with st.sidebar:
        st.title("困境资产X光机")
        st.caption("Distressed Asset Risk Review Agent")
        st.divider()
        st.markdown("##### 运行配置")
        st.metric("Eyes 视觉模型", eyes_status)
        st.caption(os.getenv("EYES_MODEL", "qwen-vl-max"))
        st.metric("Brain 推理模型", brain_status)
        st.caption(os.getenv("BRAIN_MODEL", "deepseek-reasoner"))
        st.caption("DeepSeek Key 已读取" if _is_configured(os.getenv("BRAIN_API_KEY")) else "DeepSeek Key 未读取")
        st.metric("LangSmith Trace", smith_status)
        st.caption(os.getenv("LANGCHAIN_PROJECT", "困境资产X光机_Demo"))
        st.caption(f"本地日志：{LOG_FILE}")
        st.divider()
        uploaded_files = st.file_uploader(
            "上传资产包文件（可多选）",
            type=["csv", "pdf", "png", "jpg", "jpeg", "txt", "md", "docx", "xlsx"],
            accept_multiple_files=True,
            help="浏览器控件可多选文件；若资料很多，推荐使用下方文件夹路径，系统会递归读取整个文件夹。",
        )
        folder_path = st.text_input(
            "直接读取本机资产包文件夹",
            placeholder=r"C:\Users\杨嘉禾\Desktop\某项目资料包",
            help="把所有杂乱资料放进一个文件夹后，将文件夹路径粘贴到这里。系统会递归读取子文件夹内所有支持文件。",
        )
        if uploaded_files:
            st.caption(f"已接收 {len(uploaded_files)} 个上传文件")
        if folder_path.strip():
            st.caption("已选择文件夹路径：将优先递归解析该文件夹。")
        fast_mode = st.checkbox("快速模式", value=True, help="docx/xlsx 直接抽文本，仅对少量图片/PDF 执行 OCR。")
        max_ocr_files = st.number_input(
            "最多 OCR 文件数",
            min_value=1,
            max_value=200,
            value=20,
            step=1,
            disabled=not fast_mode,
        )
        save_outputs = st.checkbox("保存本次结构化结果", value=False)
        has_input = bool(folder_path.strip()) or bool(uploaded_files)
        uploaded_all_csv = bool(uploaded_files) and all(Path(file.name).suffix.lower() == ".csv" for file in uploaded_files)
        parse_needs_brain = bool(uploaded_files) and not uploaded_all_csv
        parse_disabled = not has_input or (parse_needs_brain and brain_status != "在线")
        if parse_disabled and has_input and parse_needs_brain:
            st.warning("非纯 CSV 资料包需要先在 .env 中填写 BRAIN_API_KEY。")
        if st.button("解析项目数据并进入流程", type="primary", use_container_width=True, disabled=parse_disabled):
            with st.spinner("正在解析项目数据..."):
                try:
                    if folder_path.strip():
                        file_paths = _collect_folder_files(folder_path.strip())
                    else:
                        file_paths = _save_uploaded_files(uploaded_files)
                    if _can_parse_without_brain(file_paths):
                        structured_state, merged_markdown, parse_logs = _parse_csv_files_to_state(file_paths)
                    else:
                        if brain_status != "在线":
                            raise ValueError("非纯 CSV 资料包需要先在 .env 中填写 BRAIN_API_KEY。")
                        structured_state, merged_markdown, parse_logs = _parse_files_to_state(
                            file_paths,
                            fast_mode=fast_mode,
                            max_ocr_files=int(max_ocr_files),
                        )
                    st.session_state.project_state = structured_state
                    st.session_state.metric_result_cache = {}
                    st.session_state.report_chapter_cache = {}
                    st.session_state.ocr_markdown = merged_markdown
                    st.session_state.run_done = False
                    st.session_state.event_log = parse_logs
                    st.session_state.material_count = len(file_paths)
                    st.session_state.project_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
                    if save_outputs:
                        run_dir = _create_run_dir()
                        _save_structured_artifacts(run_dir, merged_markdown, structured_state)
                        st.session_state.run_dir = str(run_dir)
                    else:
                        st.session_state.run_dir = ""
                except Exception as exc:
                    logger.exception("Asset package parsing failed")
                    st.error(f"解析失败：{exc}")
                else:
                    logger.info("Asset package parsing succeeded | files=%s", len(file_paths))
                    st.success(f"项目数据解析完成，共处理 {len(file_paths)} 个文件，四类输入已锁定。")
        if st.button("载入演示结构化数据", use_container_width=True):
            with st.spinner("正在载入海南陵水项目样本..."):
                time.sleep(0.6)
                _inject_sample_data()
            st.success("演示数据已载入")

        st.divider()
        st.markdown("[打开 LangSmith](https://smith.langchain.com/)")

    state: ProjectState = st.session_state.project_state
    has_data = all(state.get(key) for key in SAMPLE_DATA)
    run_done = bool(st.session_state.run_done)
    pcs_score = int(state.get("pcs_score", 0)) if run_done else None
    material_count = int(st.session_state.get("material_count", 0)) if has_data else 0

    _render_overview_bar(has_data, run_done, pcs_score, material_count)

    if st.session_state.active_report_view:
        _render_overall_report_detail(state, has_data, run_done)
        return

    if st.session_state.active_dimension:
        _render_dimension_detail(state)
        return

    left_col, right_col = st.columns([3, 1], gap="large")
    with left_col:
        st.markdown("<div class='workbench-section-title'>维度总览</div>", unsafe_allow_html=True)
        if not has_data:
            with st.container(border=True):
                st.markdown("#### 等待项目材料接入")
                st.caption("请在左侧上传资料包、输入本地文件夹路径，或载入演示结构化数据。")
        _render_dimension_cards(state, has_data, run_done)

        _render_overall_report_panel(state, has_data, run_done)

        st.divider()
        st.markdown("#### 四 Agent 审阅")
        scan_disabled = not has_data or brain_status != "在线"
        if scan_disabled and has_data:
            st.warning("请先配置 BRAIN_API_KEY 或 OPENAI_API_KEY，再启动扫描。")
        if st.button("启动 X 光机扫描", type="primary", disabled=scan_disabled):
            logger.info("LangGraph scan triggered")
            final_state = _stream_graph_run(state)
            st.session_state.project_state = final_state
            st.session_state.run_done = True
            st.session_state.project_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            _pre_generate_report_chapters(final_state)
            if st.session_state.run_dir:
                _save_final_report(Path(st.session_state.run_dir), final_state)
            logger.info("LangGraph scan finished | pcs_score=%s", final_state.get("pcs_score"))
            st.rerun()

        if st.session_state.run_done:
            with st.expander("资产 Agent 专题板块", expanded=False):
                _render_agent_topic_board(
                    "资产 Agent",
                    st.session_state.project_state.get("asset_patches", []),
                    "等待资产 Agent 输出权属、抵押、查封、处置价值等专题。",
                )
            with st.expander("经济 Agent 专题板块", expanded=False):
                _render_agent_topic_board(
                    "经济 Agent",
                    st.session_state.project_state.get("economic_patches", []),
                    "等待经济 Agent 输出市场、货值、去化、回款等专题。",
                )
            with st.expander("法律 Agent 专题板块", expanded=False):
                _render_agent_topic_board(
                    "法律 Agent",
                    st.session_state.project_state.get("legal_patches", []),
                    "等待法律 Agent 输出控制权、债权顺位、查封路径等专题。",
                )
            with st.expander("财务 Agent 专题板块", expanded=False):
                _render_agent_topic_board(
                    "财务 Agent",
                    st.session_state.project_state.get("financial_patches", []),
                    "等待财务 Agent 输出货值、成本、去化、税费等专题。",
                )
        else:
            st.caption("扫描完成后会展示资产、经济、法律、财务四个 Agent 识别出的专题板块。")

        st.divider()
        st.markdown("#### PCS 智能投决宣判")
        if not st.session_state.run_done:
            st.caption("等待扫描完成后生成 PCS 分数与投决报告。")
        else:
            final_state = st.session_state.project_state
            pcs_score = int(final_state.get("pcs_score", 0))
            st.metric("PCS 投决分数", pcs_score)
            st.progress(min(max(pcs_score, 0), 100) / 100)
            _render_pcs_breakdown(final_state.get("pcs_breakdown", {}))
            if pcs_score < 50:
                st.error("红线拦截：当前项目不满足直接投资条件，必须先完成控制权、债权优先级和复工成本修复。")
            else:
                st.success("条件通过：项目具备进入交易结构设计和投资委员会复核的基础。")
            st.markdown(final_state.get("final_report", ""))
            st.divider()
            st.link_button("查看 LangSmith 运行轨迹", "https://smith.langchain.com/")
            if st.session_state.run_dir:
                st.caption(f"本次结果已保存：{st.session_state.run_dir}")

    with right_col:
        _render_project_definition(state, has_data, material_count)
        _render_process_card(has_data, run_done, pcs_score)
        if st.session_state.event_log:
            recent_events = st.session_state.event_log[-6:]
            with st.container(border=True):
                st.markdown("#### 处理日志")
                st.caption(f"{len(st.session_state.event_log)} 条事件")
                for item in recent_events:
                    st.markdown(f"- {_short_event(item)}")
        with st.container(border=True):
            st.markdown("#### 系统状态")
            st.caption("Runtime")
            st.markdown(f"**Eyes 视觉模型**  \n{eyes_status} / {os.getenv('EYES_MODEL', 'qwen-vl-max')}")
            st.markdown(f"**Brain 推理模型**  \n{brain_status} / {os.getenv('BRAIN_MODEL', 'deepseek-reasoner')}")
            st.markdown(f"**LangSmith Trace**  \n{smith_status}")


if __name__ == "__main__":
    main()
