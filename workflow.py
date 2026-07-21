from __future__ import annotations

import os
import json
import re
from functools import lru_cache
from typing import Iterable, List

import httpx
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langsmith import traceable

from logger_config import get_logger
from state import ProjectState


load_dotenv(override=True)
logger = get_logger("workflow")

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


LEGAL_PROMPT = """
你是不良资产项目的法律风控 Agent。你只能基于用户提供的两类材料判断：
1. 股权与历史沿革数据
2. 金融机构债权与查封明细

任务：寻找会影响困境资产收购、重整、代偿、债权转让或项目公司控制的底层漏洞。
重点死磕：第一顺位债权人、查封法院、轮候查封、交叉质押、公章控制权、股权代持、
历史增资瑕疵、实控人纠纷、债权优先级冲突。

输出要求：
- 只输出 3 到 7 条 Markdown bullet。
- 每条必须包含：风险标签、事实依据、投资影响、建议动作。
- 不得引用未提供材料之外的事实。
"""


FINANCIAL_PROMPT = """
你是不良资产项目的财务测算 Agent。你只能基于用户提供的两类材料判断：
1. 货值与开发成本数据
2. 资产明细与抵押物清册

任务：挤干货值水分，复核复工建安成本、税费、销售折扣、去化周期、抵押物可处置性，
计算净回收空间和利润安全垫。

输出要求：
- 只输出 3 到 7 条 Markdown bullet。
- 每条必须包含：测算标签、关键假设、金额或比例影响、建议动作。
- 对不确定估值必须主动折扣，不得乐观外推。
"""


ASSET_PROMPT = """
你是不良资产项目的资产 Agent。你只能基于用户提供的资产明细与抵押物清册判断。

任务：审查项目到底有什么资产、权属和证照是否完整、抵押/查封/受限范围是否覆盖核心资产、
实物状态是否支持复工或处置、折价后可处置价值是否可靠。

输出要求：
- 只输出 3 到 7 条 Markdown bullet。
- 每条必须包含：资产专题、事实依据、处置影响、建议动作。
- 不得引用未提供材料之外的事实。
"""


ECONOMIC_PROMPT = """
你是不良资产项目的经济 Agent。你只能基于货值与开发成本数据判断。

任务：审查项目经济性，包括货值总览、价格假设、市场对标、销售去化、回款预测、
敏感性分析和投资安全垫。重点识别乐观价格、慢去化、回款错配和清偿率承压。

输出要求：
- 只输出 3 到 7 条 Markdown bullet。
- 每条必须包含：经济专题、关键假设、金额或比例影响、建议动作。
- 对价格、去化和回款必须用审慎口径，不得乐观外推。
"""


CLOSER_PROMPT = """
你是困境资产投融资项目的双视角分析总控 Agent。你绝对不能读取原始材料，只能读取资产、经济、
法律、财务四类 Agent patches 和 Python 扣分矩阵给出的 PCS 分数。

任务：基于同一组风险事实，分别站在买方和卖方角度生成分析。买方视角要偏审慎，关注压价依据、
交割条件、风险隔离、付款节奏和退出安全垫；卖方视角要关注价值呈现、风险解释、资料补强、
谈判让步边界和提高成交确定性的路径。不得引入输入以外的新事实。

报告结构必须包含以下标题：
1. 总体结论
2. 买方视角分析
3. 卖方视角分析
4. 双方核心分歧
5. 交易推进建议

要求：
- 每个视角至少包含 3 条明确判断。
- 明确说明 PCS 分数对买方和卖方分别意味着什么。
- 对风险事实要给出可执行动作，不要只做泛泛描述。
"""


STRUCTURING_PROMPT = """
你是不良资产资料清洗与结构化分流 Agent。你会收到 PaddleOCR 提取出的原始 Markdown 文本。

任务：只基于输入文本，将内容拆分为 ProjectState 的四个输入槽：
1. equity_and_history_data：股权结构、历史沿革、实控人、印章证照、工商变更、代持、增资瑕疵。
2. financial_and_cost_data：货值、销售价格、开发成本、复工成本、税费、去化、利润、现金流。
3. creditor_and_seizure_data：金融债权、第一顺位、抵押权人、查封法院、冻结、轮候查封、工程款优先权。
4. asset_and_mortgage_data：资产清册、土地、在建工程、抵押物、楼栋、商业、物理状态、处置限制。

输出必须是严格 JSON，不要 Markdown，不要解释。JSON schema：
{
  "equity_and_history_data": "string",
  "financial_and_cost_data": "string",
  "creditor_and_seizure_data": "string",
  "asset_and_mortgage_data": "string"
}

如果某类材料缺失，填入“未在本次上传材料中识别到明确内容。”。
"""


STRUCTURING_CHUNK_PROMPT = """
你是不良资产资料清洗 Agent。你会收到资产包 OCR Markdown 的一个片段。

任务：只从本片段中抽取与以下四类有关的信息，输出严格 JSON：
{
  "equity_and_history_data": ["..."],
  "financial_and_cost_data": ["..."],
  "creditor_and_seizure_data": ["..."],
  "asset_and_mortgage_data": ["..."]
}

要求：
- 每个数组最多 8 条。
- 每条保留关键事实、金额、主体、日期、合同/证号、抵押物名称。
- 如果本片段没有对应信息，填空数组。
- 不要解释，不要 Markdown。
"""


STRUCTURING_MERGE_PROMPT = """
你是不良资产资料总整理 Agent。你会收到多个 OCR 片段已经抽取出的候选信息。

任务：去重、归并、压缩并整理成 ProjectState 的四个输入槽。

输出必须是严格 JSON：
{
  "equity_and_history_data": "string",
  "financial_and_cost_data": "string",
  "creditor_and_seizure_data": "string",
  "asset_and_mortgage_data": "string"
}

要求：
- 每个 string 用 Markdown bullet 表达。
- 优先保留主体、金额、债权关系、抵押物、法院、查封、合同编号、日期、评估值、成本等硬信息。
- 删除重复表述和无关营销文字。
- 如果某类材料缺失，填入“未在本次上传材料中识别到明确内容。”。
- 不要输出 JSON 之外的内容。
"""


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@lru_cache(maxsize=2)
def get_brain_llm() -> ChatOpenAI:
    model = _env("BRAIN_MODEL", _env("OPENAI_MODEL", "gpt-4o-mini"))
    api_key = _env("BRAIN_API_KEY", _env("OPENAI_API_KEY"))
    base_url = _env("BRAIN_BASE_URL", _env("OPENAI_BASE_URL"))

    kwargs: dict = {
        "model": model,
        "api_key": api_key or None,
        "timeout": 90,
        "max_retries": 2,
        "http_client": httpx.Client(trust_env=_bool_env("BRAIN_TRUST_ENV", False)),
    }
    if base_url:
        kwargs["base_url"] = base_url
    if not re.search(r"\bo[13]\b|o3|o4|reason", model, re.IGNORECASE):
        kwargs["temperature"] = 0
    return ChatOpenAI(**kwargs)


@lru_cache(maxsize=2)
def get_eyes_llm() -> ChatOpenAI:
    model = _env("EYES_MODEL", "gpt-4o")
    api_key = _env("EYES_API_KEY", _env("OPENAI_API_KEY"))
    base_url = _env("EYES_BASE_URL", _env("OPENAI_BASE_URL"))

    kwargs: dict = {
        "model": model,
        "api_key": api_key or None,
        "timeout": 90,
        "max_retries": 2,
        "http_client": httpx.Client(trust_env=_bool_env("EYES_TRUST_ENV", False)),
    }
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def _process_llm_trace_inputs(inputs: dict) -> dict:
    system_prompt = str(inputs.get("system_prompt") or "")
    user_payload = str(inputs.get("user_payload") or "")
    return {
        "system_prompt_chars": len(system_prompt),
        "system_prompt_preview": system_prompt[:2000],
        "user_payload_chars": len(user_payload),
        "user_payload_preview": user_payload[:12000],
        "display_note": "Trace仅展示受限预览；模型实际接收完整上下文。",
    }


def _process_llm_trace_output(output: object) -> dict:
    text = str(output or "")
    return {"response_chars": len(text), "response_preview": text[:12000]}


@traceable(
    name="brain_llm_call",
    run_type="llm",
    process_inputs=_process_llm_trace_inputs,
    process_outputs=_process_llm_trace_output,
)
def _invoke_brain(system_prompt: str, user_payload: str) -> str:
    llm = get_brain_llm()
    response = llm.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_payload),
        ]
    )
    return str(response.content).strip()


def _extract_json_object(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _chunk_text(text: str, max_chars: int = 18000) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for part in re.split(r"\n\s*---\s*\n|(?=^## 文件\s+\d+:)", text, flags=re.MULTILINE):
        part = part.strip()
        if not part:
            continue
        if len(part) > max_chars:
            for start in range(0, len(part), max_chars):
                segment = part[start : start + max_chars].strip()
                if segment:
                    chunks.append(segment)
            continue
        if current and current_len + len(part) > max_chars:
            chunks.append("\n\n---\n\n".join(current))
            current = []
            current_len = 0
        current.append(part)
        current_len += len(part)
    if current:
        chunks.append("\n\n---\n\n".join(current))
    return chunks


def _normalize_candidate_payload(payload: dict) -> dict[str, list[str]]:
    keys = [
        "equity_and_history_data",
        "financial_and_cost_data",
        "creditor_and_seizure_data",
        "asset_and_mortgage_data",
    ]
    normalized: dict[str, list[str]] = {}
    for key in keys:
        value = payload.get(key, [])
        if isinstance(value, str):
            normalized[key] = [value] if value.strip() else []
        elif isinstance(value, list):
            normalized[key] = [str(item).strip() for item in value if str(item).strip()]
        else:
            normalized[key] = []
    return normalized


def _candidate_to_markdown(candidates: list[dict[str, list[str]]]) -> str:
    keys = [
        "equity_and_history_data",
        "financial_and_cost_data",
        "creditor_and_seizure_data",
        "asset_and_mortgage_data",
    ]
    labels = {
        "equity_and_history_data": "股权与历史沿革候选信息",
        "financial_and_cost_data": "货值与开发成本候选信息",
        "creditor_and_seizure_data": "金融债权与查封候选信息",
        "asset_and_mortgage_data": "资产明细与抵押物候选信息",
    }
    sections: list[str] = []
    for key in keys:
        items: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            for item in candidate.get(key, []):
                if item not in seen:
                    seen.add(item)
                    items.append(item)
        body = "\n".join(f"- {item}" for item in items) or "- 未抽取到明确内容"
        sections.append(f"## {labels[key]}\n{body}")
    return "\n\n".join(sections)


def _state_from_payload(payload: dict) -> ProjectState:
    fallback = "未在本次上传材料中识别到明确内容。"
    return {
        "equity_and_history_data": str(payload.get("equity_and_history_data") or fallback),
        "financial_and_cost_data": str(payload.get("financial_and_cost_data") or fallback),
        "creditor_and_seizure_data": str(payload.get("creditor_and_seizure_data") or fallback),
        "asset_and_mortgage_data": str(payload.get("asset_and_mortgage_data") or fallback),
        "asset_patches": [],
        "economic_patches": [],
        "legal_patches": [],
        "financial_patches": [],
        "pcs_score": 0,
        "pcs_breakdown": {},
        "final_report": "",
    }


@traceable(name="structure_markdown_to_state", run_type="chain")
def structure_markdown_to_state(markdown: str) -> ProjectState:
    logger.info("Structuring OCR markdown started | chars=%s", len(markdown))
    direct_max_chars = int(os.getenv("STRUCTURE_DIRECT_MAX_CHARS", "50000"))
    if len(markdown) <= direct_max_chars:
        result = _invoke_brain(
            STRUCTURING_PROMPT,
            f"【PaddleOCR Markdown】\n{markdown}",
        )
        payload = _extract_json_object(result)
        structured_state = _state_from_payload(payload)
    else:
        chunk_size = int(os.getenv("STRUCTURE_CHUNK_CHARS", "18000"))
        chunks = _chunk_text(markdown, max_chars=chunk_size)
        logger.info("Structuring OCR markdown chunked | chunks=%s | chunk_size=%s", len(chunks), chunk_size)
        candidates: list[dict[str, list[str]]] = []
        for index, chunk in enumerate(chunks, start=1):
            logger.info("Structuring chunk started | index=%s/%s | chars=%s", index, len(chunks), len(chunk))
            result = _invoke_brain(
                STRUCTURING_CHUNK_PROMPT,
                f"【片段 {index}/{len(chunks)}】\n{chunk}",
            )
            payload = _extract_json_object(result)
            candidates.append(_normalize_candidate_payload(payload))
            logger.info("Structuring chunk finished | index=%s/%s", index, len(chunks))

        candidate_markdown = _candidate_to_markdown(candidates)
        logger.info("Structuring merge started | candidate_chars=%s", len(candidate_markdown))
        result = _invoke_brain(
            STRUCTURING_MERGE_PROMPT,
            f"【候选信息】\n{candidate_markdown}",
        )
        payload = _extract_json_object(result)
        structured_state = _state_from_payload(payload)

    logger.info(
        "Structuring OCR markdown finished | equity_chars=%s | financial_chars=%s | creditor_chars=%s | asset_chars=%s",
        len(structured_state["equity_and_history_data"]),
        len(structured_state["financial_and_cost_data"]),
        len(structured_state["creditor_and_seizure_data"]),
        len(structured_state["asset_and_mortgage_data"]),
    )
    return structured_state


def _split_bullets(text: str) -> List[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullets = [re.sub(r"^[-*]\s*", "", line) for line in lines if line.startswith(("-", "*"))]
    return bullets or [text.strip()]


def _process_master_inventory_output(output: object) -> dict:
    inventory = dict(output or {})
    return {
        "project_id": inventory.get("project_id"),
        "project_name": inventory.get("project_name"),
        "counts": inventory.get("counts"),
        "ai_relation_types": inventory.get("ai_relation_types"),
        "inventory_complete": inventory.get("inventory_complete"),
    }


@traceable(name="01_database_inventory", run_type="retriever", process_outputs=_process_master_inventory_output)
def _trace_master_inventory(project_id: int) -> dict:
    from graph_rag import inventory_project

    return inventory_project(project_id)


@traceable(name="02_analysis_plan", run_type="chain")
def _trace_master_plan(inventory: dict) -> dict:
    from graph_rag import build_master_plan

    return build_master_plan(inventory)


def master_planner_node(state: ProjectState) -> ProjectState:
    project_id = int(state.get("project_id") or 0)
    if not project_id or state.get("data_source") != "cloud_mysql_ai":
        return {
            "master_inventory": {"source": state.get("data_source") or "uploaded_files", "inventory_complete": False},
            "master_plan": {"strategy": "使用已装配的四维文本输入运行固定专业Agent。"},
        }
    inventory = _trace_master_inventory(project_id)
    plan = _trace_master_plan(inventory)
    return {
        "project_name": inventory.get("project_name") or state.get("project_name", ""),
        "master_inventory": inventory,
        "master_plan": plan,
    }


TRACE_PREVIEW_ITEM_LIMIT = max(1, int(_env("TRACE_PREVIEW_ITEM_LIMIT", "8")))
TRACE_PREVIEW_CHARS = max(120, int(_env("TRACE_PREVIEW_CHARS", "600")))
TRACE_TOTAL_PREVIEW_CHARS = max(1000, int(_env("TRACE_TOTAL_PREVIEW_CHARS", "5000")))


def _clip_trace_text(value: object, limit: int = TRACE_PREVIEW_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _trace_lines(text: str, patterns: tuple[str, ...] = ()) -> list[str]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if patterns:
        lines = [line for line in lines if any(pattern in line for pattern in patterns)]
    previews: list[str] = []
    total_chars = 0
    for line in lines:
        preview = _clip_trace_text(line)
        if len(previews) >= TRACE_PREVIEW_ITEM_LIMIT or total_chars + len(preview) > TRACE_TOTAL_PREVIEW_CHARS:
            break
        previews.append(preview)
        total_chars += len(preview)
    return previews


def _process_context_trace_inputs(inputs: dict) -> dict:
    sections = inputs.get("sections") or {}
    return {
        "agent": inputs.get("agent_name"),
        "sections": {name: {"chars": len(str(text or "")), "preview": _trace_lines(str(text or ""))[:2]} for name, text in sections.items()},
        "display_limits": {"items": TRACE_PREVIEW_ITEM_LIMIT, "chars_per_item": TRACE_PREVIEW_CHARS, "total_preview_chars": TRACE_TOTAL_PREVIEW_CHARS},
    }


def _context_manifest(agent_name: str, sections: dict[str, str]) -> dict:
    combined = "\n".join(sections.values())
    event_ids = list(dict.fromkeys(re.findall(r"事件#(\d+)", combined)))
    evidence_ids = list(dict.fromkeys(re.findall(r"证据#(\d+)", combined)))
    return {
        "agent": agent_name,
        "section_chars": {name: len(text) for name, text in sections.items()},
        "event_count": len(event_ids),
        "evidence_count": len(evidence_ids),
        "event_id_preview": event_ids[:TRACE_PREVIEW_ITEM_LIMIT],
        "evidence_id_preview": evidence_ids[:TRACE_PREVIEW_ITEM_LIMIT],
        "fact_previews": _trace_lines(combined, ("状态=", "当前事实")),
        "risk_previews": _trace_lines(combined, ("## 已识别风险", "[high]", "[critical]", "[medium]")),
        "truncated_for_display": True,
        "full_context_used_by_model": True,
    }


@traceable(name="01_context_selection", run_type="retriever", process_inputs=_process_context_trace_inputs)
def _trace_context_selection(agent_name: str, sections: dict[str, str]) -> dict:
    return _context_manifest(agent_name, sections)


@traceable(name="02_evidence_and_conflict_check", run_type="chain", process_inputs=_process_context_trace_inputs)
def _trace_evidence_and_conflict_check(agent_name: str, sections: dict[str, str]) -> dict:
    combined = "\n".join(sections.values())
    primary_refs = re.findall(r"(\d+):primary:(?:source|ai)", combined)
    supporting_refs = re.findall(r"(\d+):supporting:(?:source|ai)", combined)
    conflicting_refs = re.findall(r"(\d+):conflicting:(?:source|ai)", combined)
    review_lines = _trace_lines(combined, ("need_review", "待审核", "conflicting"))
    return {
        "agent": agent_name,
        "primary_evidence_count": len(set(primary_refs)),
        "supporting_evidence_count": len(set(supporting_refs)),
        "conflicting_evidence_count": len(set(conflicting_refs)),
        "primary_evidence_preview": list(dict.fromkeys(primary_refs))[:TRACE_PREVIEW_ITEM_LIMIT],
        "supporting_evidence_preview": list(dict.fromkeys(supporting_refs))[:TRACE_PREVIEW_ITEM_LIMIT],
        "conflicting_evidence_preview": list(dict.fromkeys(conflicting_refs))[:TRACE_PREVIEW_ITEM_LIMIT],
        "review_previews": review_lines,
        "analysis_rule": "冲突证据只作为风险提示并进入审核，不自动覆盖原始事实。",
    }


def _process_agent_analysis_inputs(inputs: dict) -> dict:
    payload = str(inputs.get("user_payload") or "")
    context = dict(inputs.get("context_manifest") or {})
    evidence = dict(inputs.get("evidence_check") or {})
    compact_context = {
        "agent": context.get("agent"),
        "section_chars": context.get("section_chars"),
        "event_count": context.get("event_count"),
        "evidence_count": context.get("evidence_count"),
        "event_id_preview": (context.get("event_id_preview") or [])[:TRACE_PREVIEW_ITEM_LIMIT],
        "evidence_id_preview": (context.get("evidence_id_preview") or [])[:TRACE_PREVIEW_ITEM_LIMIT],
        "fact_previews": [_clip_trace_text(item, 320) for item in (context.get("fact_previews") or [])[:3]],
        "risk_previews": [_clip_trace_text(item, 320) for item in (context.get("risk_previews") or [])[:3]],
    }
    compact_evidence = {
        "primary_evidence_count": evidence.get("primary_evidence_count"),
        "supporting_evidence_count": evidence.get("supporting_evidence_count"),
        "conflicting_evidence_count": evidence.get("conflicting_evidence_count"),
        "conflicting_evidence_preview": (evidence.get("conflicting_evidence_preview") or [])[:TRACE_PREVIEW_ITEM_LIMIT],
        "review_previews": [_clip_trace_text(item, 320) for item in (evidence.get("review_previews") or [])[:3]],
    }
    return {
        "agent": inputs.get("agent_name"),
        "task": inputs.get("task"),
        "context_manifest": compact_context,
        "evidence_check": compact_evidence,
        "model_payload_chars": len(payload),
        "model_payload_preview": payload[:8000],
        "display_note": "完整文本已送入模型；Trace输入控制在约15,000字符内。",
    }

def _process_agent_analysis_output(output: object) -> dict:
    text = str(output or "")
    return {"analysis_chars": len(text), "analysis_preview": text[:TRACE_TOTAL_PREVIEW_CHARS]}


@traceable(name="03_agent_analysis", run_type="chain", process_inputs=_process_agent_analysis_inputs, process_outputs=_process_agent_analysis_output)
def _trace_agent_analysis(
    agent_name: str,
    task: str,
    system_prompt: str,
    user_payload: str,
    context_manifest: dict,
    evidence_check: dict,
) -> str:
    return _invoke_brain(system_prompt, user_payload)


def _risk_level_for_finding(text: str) -> str:
    if any(keyword in text for keyword in ("红线", "重大", "无法", "严重", "第一顺位", "查封", "冲突")):
        return "high"
    if any(keyword in text for keyword in ("风险", "不足", "偏高", "待核", "不确定", "限制")):
        return "medium"
    return "low"


def _process_conclusion_inputs(inputs: dict) -> dict:
    analysis_text = str(inputs.get("analysis_text") or "")
    return {"agent": inputs.get("agent_name"), "analysis_chars": len(analysis_text), "analysis_preview": analysis_text[:TRACE_TOTAL_PREVIEW_CHARS]}


@traceable(name="04_agent_conclusion", run_type="chain", process_inputs=_process_conclusion_inputs)
def _trace_agent_conclusion(agent_name: str, analysis_text: str) -> dict:
    patches = _split_bullets(analysis_text)
    findings = []
    for index, patch in enumerate(patches[:TRACE_PREVIEW_ITEM_LIMIT], start=1):
        evidence_ids = list(dict.fromkeys(re.findall(r"(?:证据|evidence)[#：:\s]*(\d+)", patch, re.IGNORECASE)))
        event_ids = list(dict.fromkeys(re.findall(r"(?:事件|event)[#：:\s]*(\d+)", patch, re.IGNORECASE)))
        title, _, detail = patch.partition("：")
        findings.append({
            "finding_no": index,
            "title": _clip_trace_text(title or patch, 160),
            "risk_level": _risk_level_for_finding(patch),
            "analysis_summary": _clip_trace_text(detail or patch),
            "event_ids": event_ids[:TRACE_PREVIEW_ITEM_LIMIT],
            "evidence_ids": evidence_ids[:TRACE_PREVIEW_ITEM_LIMIT],
        })
    return {"agent": agent_name, "patch_count": len(patches), "findings": findings, "patches": patches}


def _process_dynamic_retrieval_output(output: object) -> dict:
    payload = dict(output or {})
    sections = payload.get("sections") or {}
    report = payload.get("report") or {}
    return {
        "agent": report.get("agent"),
        "task": report.get("task"),
        "section_chars": {name: len(str(text or "")) for name, text in sections.items()},
        "coverage": {
            "events_scanned": report.get("events_scanned"),
            "events_selected": report.get("events_selected"),
            "evidences_scanned": report.get("evidences_scanned"),
            "evidences_selected": report.get("evidences_selected"),
            "graph_relations_scanned": report.get("graph_relations_scanned"),
            "entity_relations_scanned": report.get("entity_relations_scanned"),
            "raw_contents_evaluated_by_database": report.get("raw_contents_evaluated_by_database"),
            "raw_keyword_matches": report.get("raw_keyword_matches"),
            "raw_contents_selected_for_model": report.get("raw_contents_selected_for_model"),
        },
        "selected_event_id_preview": (report.get("selected_event_ids") or [])[:TRACE_PREVIEW_ITEM_LIMIT],
        "selected_evidence_id_preview": (report.get("selected_evidence_ids") or [])[:TRACE_PREVIEW_ITEM_LIMIT],
        "selected_raw_content_id_preview": (report.get("selected_raw_content_ids") or [])[:TRACE_PREVIEW_ITEM_LIMIT],
        "selection_method": report.get("selection_method"),
    }


@traceable(name="01_context_selection", run_type="retriever", process_outputs=_process_dynamic_retrieval_output)
def _trace_dynamic_context_selection(project_id: int, agent_name: str, master_plan: dict) -> dict:
    from graph_rag import retrieve_agent_context

    return retrieve_agent_context(project_id, agent_name)


def _run_agent(
    state: ProjectState,
    agent_name: str,
    task: str,
    system_prompt: str,
    fallback_sections: dict[str, str],
) -> tuple[list[str], dict]:
    project_id = int(state.get("project_id") or 0)
    if project_id and state.get("data_source") == "cloud_mysql_ai":
        retrieval = _trace_dynamic_context_selection(project_id, agent_name, state.get("master_plan", {}))
        sections = dict(retrieval.get("sections") or {})
        retrieval_report = dict(retrieval.get("report") or {})
        context_manifest = {
            "agent": agent_name,
            "section_chars": {name: len(text) for name, text in sections.items()},
            "event_count": retrieval_report.get("events_selected", 0),
            "evidence_count": retrieval_report.get("evidences_selected", 0),
            "event_id_preview": (retrieval_report.get("selected_event_ids") or [])[:TRACE_PREVIEW_ITEM_LIMIT],
            "evidence_id_preview": (retrieval_report.get("selected_evidence_ids") or [])[:TRACE_PREVIEW_ITEM_LIMIT],
            "fact_previews": _trace_lines("\n".join(sections.values()), ("[事实#",)),
            "risk_previews": _trace_lines("\n".join(sections.values()), ("[风险#",)),
        }
    else:
        sections = fallback_sections
        context_manifest = _trace_context_selection(agent_name, sections)
        retrieval_report = {
            "agent": agent_name,
            "mode": "preassembled_context",
            "full_structured_scan_completed": False,
            "selected_event_ids": context_manifest.get("event_id_preview", []),
            "selected_evidence_ids": context_manifest.get("evidence_id_preview", []),
        }
    evidence_check = _trace_evidence_and_conflict_check(agent_name, sections)
    user_payload = "\n\n".join(f"【{title}】\n{text}" for title, text in sections.items())
    analysis_text = _trace_agent_analysis(agent_name, task, system_prompt, user_payload, context_manifest, evidence_check)
    conclusion = _trace_agent_conclusion(agent_name, analysis_text)
    return list(conclusion["patches"]), retrieval_report


def asset_agent_node(state: ProjectState) -> ProjectState:
    logger.info("Asset agent started")
    patches, report = _run_agent(state, "asset_agent", "核验资产权属、抵押质押、处置限制和资产价值风险", ASSET_PROMPT, {"资产明细与抵押物清册": state.get("asset_and_mortgage_data", "")})
    logger.info("Asset agent finished | patches=%s", len(patches))
    return {"asset_patches": patches, "asset_retrieval_report": report}


def economic_agent_node(state: ProjectState) -> ProjectState:
    logger.info("Economic agent started")
    patches, report = _run_agent(state, "economic_agent", "核验货值、市场价格、销售去化和回款风险", ECONOMIC_PROMPT, {"货值与开发成本数据": state.get("financial_and_cost_data", "")})
    logger.info("Economic agent finished | patches=%s", len(patches))
    return {"economic_patches": patches, "economic_retrieval_report": report}


def legal_agent_node(state: ProjectState) -> ProjectState:
    logger.info("Legal agent started")
    patches, report = _run_agent(state, "legal_agent", "核验股权控制、债权顺位、查封诉讼和担保实现风险", LEGAL_PROMPT, {"股权与历史沿革数据": state.get("equity_and_history_data", ""), "金融机构债权与查封明细": state.get("creditor_and_seizure_data", "")})
    logger.info("Legal agent finished | patches=%s", len(patches))
    return {"legal_patches": patches, "legal_retrieval_report": report}


def financial_node(state: ProjectState) -> ProjectState:
    logger.info("Financial agent started")
    patches, report = _run_agent(state, "financial_agent", "核验成本、税费、利润、安全垫和资金缺口", FINANCIAL_PROMPT, {"货值与开发成本数据": state.get("financial_and_cost_data", ""), "资产明细与抵押物清册": state.get("asset_and_mortgage_data", "")})
    logger.info("Financial agent finished | patches=%s", len(patches))
    return {"financial_patches": patches, "financial_retrieval_report": report}

RISK_DEDUCTION_MATRIX: tuple[tuple[str, int], ...] = (
    ("交叉质押", 20),
    ("公章缺失", 25),
    ("公章控制权", 25),
    ("第一顺位", 18),
    ("轮候查封", 15),
    ("查封法院", 12),
    ("股权代持", 15),
    ("实控人纠纷", 15),
    ("增资瑕疵", 10),
    ("货值水分", 15),
    ("估值虚高", 15),
    ("复工成本", 12),
    ("建安成本", 12),
    ("去化周期", 10),
    ("回款", 10),
    ("价格假设", 10),
    ("市场对标", 8),
    ("税费", 8),
    ("抵押物", 10),
    ("权属", 12),
    ("证照", 10),
    ("处置限制", 10),
)

DIMENSION_RISK_CONFIG: dict[str, dict] = {
    "asset": {
        "label": "资产",
        "cap": 25,
        "keywords": (
            ("权属", 8),
            ("证照", 6),
            ("抵押物", 6),
            ("查封", 8),
            ("处置限制", 7),
            ("受限", 6),
            ("瑕疵", 5),
        ),
    },
    "economic": {
        "label": "经济",
        "cap": 25,
        "keywords": (
            ("货值水分", 8),
            ("估值虚高", 8),
            ("价格假设", 6),
            ("市场对标", 5),
            ("去化周期", 6),
            ("回款", 6),
            ("折扣", 4),
        ),
    },
    "legal": {
        "label": "法律",
        "cap": 30,
        "keywords": (
            ("第一顺位", 8),
            ("轮候查封", 7),
            ("查封法院", 6),
            ("公章控制权", 8),
            ("股权代持", 6),
            ("实控人纠纷", 6),
            ("交叉质押", 7),
            ("工程款优先权", 6),
        ),
    },
    "financial": {
        "label": "财务",
        "cap": 20,
        "keywords": (
            ("复工成本", 6),
            ("建安成本", 6),
            ("税费", 5),
            ("资金缺口", 6),
            ("清偿率", 5),
            ("安全垫", 5),
            ("债务", 5),
        ),
    },
}


def calculate_pcs_score(patches: Iterable[str]) -> int:
    joined = "\n".join(patches)
    deduction = 0
    matched_keywords: set[str] = set()
    for keyword, points in RISK_DEDUCTION_MATRIX:
        if keyword in joined and keyword not in matched_keywords:
            deduction += points
            matched_keywords.add(keyword)
    return max(0, min(100, 100 - deduction))


def calculate_dimension_deduction(patches: Iterable[str], dimension: str) -> dict:
    joined = "\n".join(patches)
    config = DIMENSION_RISK_CONFIG[dimension]
    raw_deduction = 0
    matched: list[dict[str, int | str]] = []
    seen: set[str] = set()
    for keyword, points in config["keywords"]:
        if keyword in joined and keyword not in seen:
            raw_deduction += int(points)
            matched.append({"keyword": keyword, "points": int(points)})
            seen.add(keyword)
    capped_deduction = min(int(config["cap"]), raw_deduction)
    return {
        "label": config["label"],
        "cap": int(config["cap"]),
        "raw_deduction": raw_deduction,
        "deduction": capped_deduction,
        "matched": matched,
    }


def calculate_pcs_breakdown(
    asset_patches: Iterable[str],
    economic_patches: Iterable[str],
    legal_patches: Iterable[str],
    financial_patches: Iterable[str],
) -> tuple[int, dict]:
    breakdown = {
        "asset": calculate_dimension_deduction(asset_patches, "asset"),
        "economic": calculate_dimension_deduction(economic_patches, "economic"),
        "legal": calculate_dimension_deduction(legal_patches, "legal"),
        "financial": calculate_dimension_deduction(financial_patches, "financial"),
    }
    total_deduction = sum(item["deduction"] for item in breakdown.values())
    pcs_score = max(0, min(100, 100 - total_deduction))
    breakdown["total_deduction"] = total_deduction
    breakdown["score"] = pcs_score
    return pcs_score, breakdown


def _process_findings_inputs(inputs: dict) -> dict:
    return {
        "asset_patch_count": len(inputs.get("asset_patches") or []),
        "economic_patch_count": len(inputs.get("economic_patches") or []),
        "legal_patch_count": len(inputs.get("legal_patches") or []),
        "financial_patch_count": len(inputs.get("financial_patches") or []),
        "previews": {
            "asset": [_clip_trace_text(item) for item in (inputs.get("asset_patches") or [])[:TRACE_PREVIEW_ITEM_LIMIT]],
            "economic": [_clip_trace_text(item) for item in (inputs.get("economic_patches") or [])[:TRACE_PREVIEW_ITEM_LIMIT]],
            "legal": [_clip_trace_text(item) for item in (inputs.get("legal_patches") or [])[:TRACE_PREVIEW_ITEM_LIMIT]],
            "financial": [_clip_trace_text(item) for item in (inputs.get("financial_patches") or [])[:TRACE_PREVIEW_ITEM_LIMIT]],
        },
    }


@traceable(name="01_consolidate_findings", run_type="chain", process_inputs=_process_findings_inputs)
def _trace_consolidate_findings(
    asset_patches: list[str],
    economic_patches: list[str],
    legal_patches: list[str],
    financial_patches: list[str],
) -> dict:
    return {
        "total_findings": len(asset_patches) + len(economic_patches) + len(legal_patches) + len(financial_patches),
        "dimension_counts": {
            "asset": len(asset_patches),
            "economic": len(economic_patches),
            "legal": len(legal_patches),
            "financial": len(financial_patches),
        },
        "high_risk_previews": [
            {"dimension": dimension, "finding": _clip_trace_text(patch)}
            for dimension, patches in (
                ("asset", asset_patches),
                ("economic", economic_patches),
                ("legal", legal_patches),
                ("financial", financial_patches),
            )
            for patch in patches
            if _risk_level_for_finding(patch) == "high"
        ][:TRACE_PREVIEW_ITEM_LIMIT],
    }


@traceable(name="02_calculate_pcs", run_type="chain", process_inputs=_process_findings_inputs)
def _trace_calculate_pcs(
    asset_patches: list[str],
    economic_patches: list[str],
    legal_patches: list[str],
    financial_patches: list[str],
) -> dict:
    pcs_score, pcs_breakdown = calculate_pcs_breakdown(asset_patches, economic_patches, legal_patches, financial_patches)
    return {
        "starting_score": 100,
        "dimension_deductions": pcs_breakdown,
        "total_deduction": pcs_breakdown.get("total_deduction", 0),
        "final_score": pcs_score,
    }


def _process_coverage_output(output: object) -> dict:
    coverage = dict(output or {})
    return {
        "project_id": coverage.get("project_id"),
        "project_name": coverage.get("project_name"),
        "agents_completed": coverage.get("agents_completed"),
        "structured_scan": coverage.get("structured_scan"),
        "raw_content_scan": coverage.get("raw_content_scan"),
        "unprocessed_structured_records": coverage.get("unprocessed_structured_records"),
        "interpretation": coverage.get("interpretation"),
    }


@traceable(name="03_coverage_audit", run_type="chain", process_outputs=_process_coverage_output)
def _trace_coverage_report(inventory: dict, reports: list[dict]) -> dict:
    from graph_rag import combine_coverage

    return combine_coverage(inventory, reports)


def _process_decision_inputs(inputs: dict) -> dict:
    payload = str(inputs.get("user_payload") or "")
    findings = dict(inputs.get("findings_summary") or {})
    pcs = dict(inputs.get("pcs_summary") or {})
    return {
        "pcs_summary": {
            "starting_score": pcs.get("starting_score"),
            "dimension_deductions": pcs.get("dimension_deductions"),
            "total_deduction": pcs.get("total_deduction"),
            "final_score": pcs.get("final_score"),
        },
        "findings_summary": {
            "total_findings": findings.get("total_findings"),
            "dimension_counts": findings.get("dimension_counts"),
            "high_risk_previews": (findings.get("high_risk_previews") or [])[:4],
        },
        "decision_payload_chars": len(payload),
        "decision_payload_preview": payload[:8000],
        "display_note": "最终报告使用完整四维结论；Trace输入控制在约15,000字符内。",
    }

@traceable(name="04_investment_decision", run_type="chain", process_inputs=_process_decision_inputs, process_outputs=_process_agent_analysis_output)
def _trace_investment_decision(
    user_payload: str,
    findings_summary: dict,
    pcs_summary: dict,
) -> str:
    return _invoke_brain(CLOSER_PROMPT, user_payload)


def closer_node(state: ProjectState) -> ProjectState:
    logger.info("Closer started")
    asset_patches = state.get("asset_patches", [])
    economic_patches = state.get("economic_patches", [])
    legal_patches = state.get("legal_patches", [])
    financial_patches = state.get("financial_patches", [])
    findings_summary = _trace_consolidate_findings(asset_patches, economic_patches, legal_patches, financial_patches)
    pcs_summary = _trace_calculate_pcs(asset_patches, economic_patches, legal_patches, financial_patches)
    pcs_score = int(pcs_summary["final_score"])
    pcs_breakdown = dict(pcs_summary["dimension_deductions"])
    retrieval_reports = [
        state.get("asset_retrieval_report", {}),
        state.get("economic_retrieval_report", {}),
        state.get("legal_retrieval_report", {}),
        state.get("financial_retrieval_report", {}),
    ]
    coverage_report = _trace_coverage_report(state.get("master_inventory", {}), retrieval_reports)

    user_payload = f"""
【PCS 分数】
{pcs_score}

【PCS 扣分明细】
{json.dumps(pcs_breakdown, ensure_ascii=False)}

【数据覆盖率】
{json.dumps(coverage_report, ensure_ascii=False)}

【资产 patches】
{chr(10).join(f"- {item}" for item in asset_patches)}

【经济 patches】
{chr(10).join(f"- {item}" for item in economic_patches)}

【法律 patches】
{chr(10).join(f"- {item}" for item in legal_patches)}

【财务 patches】
{chr(10).join(f"- {item}" for item in financial_patches)}
"""
    final_report = _trace_investment_decision(user_payload, findings_summary, pcs_summary)
    logger.info(
        "Closer finished | pcs_score=%s | asset_patches=%s | economic_patches=%s | legal_patches=%s | financial_patches=%s | report_chars=%s",
        pcs_score,
        len(asset_patches),
        len(economic_patches),
        len(legal_patches),
        len(financial_patches),
        len(final_report),
    )
    return {"pcs_score": pcs_score, "pcs_breakdown": pcs_breakdown, "coverage_report": coverage_report, "final_report": final_report}

def build_graph():
    graph = StateGraph(ProjectState)
    graph.add_node("master_planner_node", master_planner_node)
    graph.add_node("asset_agent_node", asset_agent_node)
    graph.add_node("economic_agent_node", economic_agent_node)
    graph.add_node("legal_agent_node", legal_agent_node)
    graph.add_node("financial_node", financial_node)
    graph.add_node("closer_node", closer_node)

    graph.add_edge(START, "master_planner_node")
    graph.add_edge("master_planner_node", "asset_agent_node")
    graph.add_edge("master_planner_node", "economic_agent_node")
    graph.add_edge("master_planner_node", "legal_agent_node")
    graph.add_edge("master_planner_node", "financial_node")
    graph.add_edge(["asset_agent_node", "economic_agent_node", "legal_agent_node", "financial_node"], "closer_node")
    graph.add_edge("closer_node", END)
    return graph.compile()


compiled_graph = build_graph()
