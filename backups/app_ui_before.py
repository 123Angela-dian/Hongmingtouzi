from __future__ import annotations

import json
import os
import csv
import time
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

from workflow import compiled_graph, structure_markdown_to_state

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
        st.markdown(f"<div class='patch-box'>{escape(item)}</div>", unsafe_allow_html=True)


def _inject_sample_data() -> None:
    state = empty_state()
    state.update(SAMPLE_DATA)
    st.session_state.project_state = state
    st.session_state.run_done = False
    st.session_state.event_log = ["演示样本已载入：海南陵水项目四类结构化文本已分流。"]
    st.session_state.ocr_markdown = ""
    st.session_state.run_dir = ""


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
    logger.info("Folder files collected | folder=%s | files=%s", root, len(files))
    return sorted(files)


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


def _parse_csv_files_to_state(file_paths: list[Path]) -> tuple[ProjectState, str, list[str]]:
    selected, logs = _select_csv_files(file_paths)
    state = empty_state()
    markdown_chunks: list[str] = []
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
        markdown_chunks.append(markdown)
        logs.append(f"{config['label']} 解析完成：{len(rows)} 条记录")
    merged_markdown = "\n\n---\n\n".join(markdown_chunks).strip()
    if not merged_markdown:
        raise ValueError("未识别到可解析的四类 CSV 文件。")
    logger.info("CSV package parsing finished | csv_categories=%s", ",".join(selected.keys()))
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
            if suffix in TEXT_EXTENSIONS:
                text = file_path.read_text(encoding="utf-8", errors="ignore").strip()
                if text:
                    markdown_chunks.append(f"{title}\n\n{text}")
                    logs.append(f"直接读取文本：{file_path.name}")
                continue

            if suffix == ".docx":
                text = _read_docx_text(file_path)
                if text:
                    markdown_chunks.append(f"{title}\n\n{text}")
                    logs.append(f"DOCX 直接抽文本：{file_path.name}")
                continue

            if suffix == ".xlsx":
                text = _read_xlsx_text(file_path)
                if text:
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
    return structured_state, merged_markdown, logs


def _stream_graph_run(initial_state: ProjectState) -> ProjectState:
    legal_box = st.empty()
    financial_box = st.empty()
    closer_box = st.empty()
    run_state: ProjectState = {
        **initial_state,
        "legal_patches": [],
        "financial_patches": [],
        "pcs_score": 0,
        "final_report": "",
    }
    final_state: ProjectState = dict(run_state)

    with st.spinner("LangGraph 正在并发调度法律与财务 Agent..."):
        graph_config = {
            "run_name": "distressed_asset_xray_graph",
            "metadata": {
                "app": "困境资产X光机",
                "project": os.getenv("LANGSMITH_PROJECT") or os.getenv("LANGCHAIN_PROJECT"),
            },
        }
        for event in compiled_graph.stream(run_state, stream_mode="updates", config=graph_config):
            st.session_state.event_log.append(str(event))
            if "legal_agent_node" in event:
                patches = event["legal_agent_node"].get("legal_patches", [])
                final_state["legal_patches"] = final_state.get("legal_patches", []) + patches
                legal_box.markdown(
                    "#### 法律 Agent Patches\n"
                    + "\n".join(f"<div class='patch-box'>{escape(item)}</div>" for item in patches),
                    unsafe_allow_html=True,
                )
            if "financial_node" in event:
                patches = event["financial_node"].get("financial_patches", [])
                final_state["financial_patches"] = final_state.get("financial_patches", []) + patches
                financial_box.markdown(
                    "#### 财务 Agent Patches\n"
                    + "\n".join(f"<div class='patch-box'>{escape(item)}</div>" for item in patches),
                    unsafe_allow_html=True,
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

    eyes_status, brain_status, smith_status = _api_status()

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
            "上传资产包文件",
            type=["csv", "pdf", "png", "jpg", "jpeg", "txt", "md", "docx", "xlsx"],
            accept_multiple_files=True,
            help="推荐直接上传四类 CSV：法律、经济财务、资产、其他。也兼容 PDF/图片/Word/Excel 资料包。",
        )
        folder_path = st.text_input(
            "或输入本机资产包文件夹路径",
            placeholder=r"C:\Users\...\项目资料包",
            help="本地运行时可直接输入文件夹路径，系统会递归读取支持的文件类型。",
        )
        if uploaded_files:
            st.caption(f"已接收 {len(uploaded_files)} 个上传文件")
        if folder_path.strip():
            st.caption("将优先解析该文件夹路径。")
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
                    st.session_state.ocr_markdown = merged_markdown
                    st.session_state.run_done = False
                    st.session_state.event_log = parse_logs
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

    st.markdown(
        """
        <div class="x-header">
            <div class="x-title">困境资产X光机：风险审查与投资决策 Demo</div>
            <p class="x-subtitle">上传多个文件，或输入本机资产包文件夹路径；系统批量识别后合并为 Markdown，再由 DeepSeek 分流到四类输入槽并完成法律、财务与投决会审。默认不保存中间结果。</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    state: ProjectState = st.session_state.project_state
    has_data = all(state.get(key) for key in SAMPLE_DATA)

    st.subheader("1. 数据解构分流")
    if not has_data:
        st.info("请先在左侧上传四类 CSV，或输入本机资料文件夹路径并解析。")
    else:
        tabs = st.tabs(["其他/项目基础", "经济财务", "法律", "资产"])
        tab_keys = list(SAMPLE_DATA.keys())
        for tab, key in zip(tabs, tab_keys):
            with tab:
                st.markdown(state.get(key, ""))

    st.subheader("2. 双 Agent 并发会审")
    scan_disabled = not has_data or brain_status != "在线"
    if scan_disabled and has_data:
        st.warning("请先配置 BRAIN_API_KEY 或 OPENAI_API_KEY，再启动扫描。")
    if st.button("启动 X 光机扫描", type="primary", disabled=scan_disabled):
        logger.info("LangGraph scan triggered")
        final_state = _stream_graph_run(state)
        st.session_state.project_state = final_state
        st.session_state.run_done = True
        if st.session_state.run_dir:
            _save_final_report(Path(st.session_state.run_dir), final_state)
        logger.info("LangGraph scan finished | pcs_score=%s", final_state.get("pcs_score"))
        st.rerun()

    if st.session_state.run_done:
        col1, col2 = st.columns(2)
        with col1:
            _patch_list("法律 Agent Patches", st.session_state.project_state.get("legal_patches", []))
        with col2:
            _patch_list("财务 Agent Patches", st.session_state.project_state.get("financial_patches", []))

    st.subheader("3. PCS 智能投决宣判")
    if not st.session_state.run_done:
        st.caption("等待扫描完成后生成 PCS 分数与投决报告。")
    else:
        final_state = st.session_state.project_state
        pcs_score = int(final_state.get("pcs_score", 0))
        st.markdown("<div class='decision-panel'>", unsafe_allow_html=True)
        score_col, verdict_col = st.columns([1, 3])
        with score_col:
            st.metric("PCS 投决分数", pcs_score)
            st.progress(min(max(pcs_score, 0), 100) / 100)
        with verdict_col:
            if pcs_score < 50:
                st.error("红线拦截：当前项目不满足直接投资条件，必须先完成控制权、债权优先级和复工成本修复。")
            else:
                st.success("条件通过：项目具备进入交易结构设计和投资委员会复核的基础。")
        st.markdown("</div>", unsafe_allow_html=True)

        report_placeholder = st.empty()
        report = final_state.get("final_report", "")
        rendered = ""
        for chunk in report.split("\n"):
            rendered += chunk + "\n"
            report_placeholder.markdown(rendered)
            time.sleep(0.03)

        st.divider()
        st.link_button("查看 LangSmith 运行轨迹", "https://smith.langchain.com/")
        if st.session_state.run_dir:
            st.caption(f"本次结果已保存：{st.session_state.run_dir}")


if __name__ == "__main__":
    main()
