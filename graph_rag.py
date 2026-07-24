from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

SOURCE_DB = "hongming01"
AI_DB = "hongming_ai"

AGENT_CONFIG = {
    "asset_agent": {
        "event_types": {"mortgage_or_pledge", "seizure_or_litigation", "valuation_or_projection", "approval_or_transaction_plan", "project_operation"},
        "evidence_types": {"asset", "mortgage", "pledge", "approval", "contract", "operation", "risk_signal"},
        "keywords": ["资产", "土地", "地块", "抵押", "质押", "查封", "权属", "建筑面积", "未售面积", "处置", "评估", "在建工程"],
        "task": "核验资产权属、抵押质押、查封限制、处置条件和资产价值。",
    },
    "economic_agent": {
        "event_types": {"sales_update", "valuation_or_projection", "project_operation"},
        "evidence_types": {"sales", "financial_metric", "cost", "asset", "operation", "risk_signal"},
        "keywords": ["货值", "销售", "去化", "回款", "均价", "市场", "未售", "收入", "利润", "现金流", "估值", "价格"],
        "task": "核验货值、市场价格、销售去化、回款和经济可行性。",
    },
    "legal_agent": {
        "event_types": {"equity_change", "claim_formation", "claim_transfer", "repayment_or_offset", "mortgage_or_pledge", "seizure_or_litigation", "contract_signing_or_termination", "approval_or_transaction_plan"},
        "evidence_types": {"equity", "claim", "mortgage", "pledge", "legal_dispute", "contract", "approval", "risk_signal"},
        "keywords": ["股权", "债权", "债务", "查封", "诉讼", "法院", "抵押", "质押", "顺位", "转让", "合同", "担保", "控制权"],
        "task": "核验股权控制、债权顺位、查封诉讼、合同效力和担保实现。",
    },
    "financial_agent": {
        "event_types": {"claim_formation", "repayment_or_offset", "sales_update", "valuation_or_projection", "project_operation"},
        "evidence_types": {"financial_metric", "cost", "sales", "claim", "asset", "risk_signal"},
        "keywords": ["成本", "税费", "利润", "资金缺口", "现金流", "债权余额", "利息", "罚息", "收购价", "货值", "偿债", "安全垫"],
        "task": "核验成本、税费、利润、现金流、偿债覆盖和资金缺口。",
    },
}


def clean_text(value: Any, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if not limit or len(text) <= limit else text[: limit - 1] + "…"


def database_config(env_path: Path = Path(".env")) -> dict[str, Any]:
    load_dotenv(env_path, override=True)
    values = dotenv_values(env_path)
    required = {
        "host": values.get("DB_HOST") or os.getenv("DB_HOST"),
        "port": values.get("DB_PORT") or os.getenv("DB_PORT"),
        "user": values.get("DB_USER") or os.getenv("DB_USER"),
        "password": values.get("DB_PASSWORD") or os.getenv("DB_PASSWORD"),
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise ValueError(f"数据库配置缺失：{', '.join(missing)}")
    required["port"] = int(required["port"])
    return required


def connect(env_path: Path = Path(".env")):
    import pymysql

    config = database_config(env_path)
    return pymysql.connect(
        host=config["host"], port=config["port"], user=config["user"], password=config["password"],
        charset="utf8mb4", connect_timeout=10, read_timeout=180, write_timeout=30, autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def inventory_project(project_id: int, env_path: Path = Path(".env")) -> dict[str, Any]:
    connection = connect(env_path)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT id,project_name,created_at,updated_at FROM {SOURCE_DB}.projects WHERE id=%s", (project_id,))
            project = cursor.fetchone()
            if not project:
                raise ValueError(f"项目不存在：projects.id={project_id}")
            counts = {}
            for table in ["files", "raw_contents", "evidences", "events", "event_evidences", "current_facts", "risks"]:
                cursor.execute(f"SELECT COUNT(*) AS count FROM {SOURCE_DB}.{table} WHERE project_id=%s", (project_id,))
                counts[table] = int(cursor.fetchone()["count"])
            for table in ["ai_entities", "ai_entity_aliases", "ai_event_entities", "ai_event_evidences", "ai_review_queue"]:
                cursor.execute(f"SELECT COUNT(*) AS count FROM {AI_DB}.{table} WHERE project_id=%s", (project_id,))
                counts[table] = int(cursor.fetchone()["count"])
            cursor.execute(f"SELECT relation_type,COUNT(*) AS count FROM {AI_DB}.ai_event_evidences WHERE project_id=%s GROUP BY relation_type", (project_id,))
            relation_types = {row["relation_type"]: int(row["count"]) for row in cursor.fetchall()}
            return {
                "project_id": project_id,
                "project_name": project["project_name"],
                "counts": counts,
                "ai_relation_types": relation_types,
                "inventory_complete": True,
                "source_database": SOURCE_DB,
                "derived_database": AI_DB,
            }
    finally:
        connection.close()


def build_master_plan(inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": inventory["project_id"],
        "project_name": inventory["project_name"],
        "strategy": "全量结构化扫描 + 图关系增强 + 原文关键词回查 + 分维度相关性排序",
        "agents": {name: {"task": config["task"], "scan_scope": "全部事件、全部证据、全部事实风险、全部关系；相关记录进入模型上下文"} for name, config in AGENT_CONFIG.items()},
        "coverage_requirements": {
            "events": "全部参与规则评分",
            "evidences": "全部参与规则和图关系评分",
            "raw_contents": "全部进入数据库关键词条件扫描，命中原文按相关性回查",
            "facts_and_risks": "全部进入各Agent上下文",
            "excluded_records": "记录数量和未选入模型原因",
        },
    }


def load_planning_snapshot(project_id: int, env_path: Path = Path(".env")) -> dict[str, Any]:
    """Return bounded project signals used by the game Master to plan retrieval."""

    inventory = inventory_project(project_id, env_path)
    connection = connect(env_path)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT event_type,COUNT(*) AS count FROM {SOURCE_DB}.events "
                "WHERE project_id=%s GROUP BY event_type ORDER BY count DESC",
                (project_id,),
            )
            event_types = {row["event_type"]: int(row["count"]) for row in cursor.fetchall()}
            cursor.execute(
                f"SELECT evidence_type,COUNT(*) AS count FROM {SOURCE_DB}.evidences "
                "WHERE project_id=%s GROUP BY evidence_type ORDER BY count DESC",
                (project_id,),
            )
            evidence_types = {row["evidence_type"]: int(row["count"]) for row in cursor.fetchall()}
            cursor.execute(
                f"SELECT id,fact_key,value_text,status,confidence,need_review FROM {SOURCE_DB}.current_facts "
                "WHERE project_id=%s ORDER BY need_review DESC,id LIMIT 80",
                (project_id,),
            )
            facts = [
                {
                    "id": int(row["id"]),
                    "fact_key": clean_text(row.get("fact_key"), 160),
                    "value": clean_text(row.get("value_text"), 500),
                    "status": row.get("status"),
                    "confidence": row.get("confidence"),
                    "need_review": bool(row.get("need_review")),
                }
                for row in cursor.fetchall()
            ]
            cursor.execute(
                f"SELECT id,risk_type,risk_title,risk_summary,severity,need_review FROM {SOURCE_DB}.risks "
                "WHERE project_id=%s ORDER BY FIELD(severity,'critical','high','medium','low'),need_review DESC,id LIMIT 60",
                (project_id,),
            )
            risks = [
                {
                    "id": int(row["id"]),
                    "risk_type": row.get("risk_type"),
                    "title": clean_text(row.get("risk_title"), 200),
                    "summary": clean_text(row.get("risk_summary"), 500),
                    "severity": row.get("severity"),
                    "need_review": bool(row.get("need_review")),
                }
                for row in cursor.fetchall()
            ]
            cursor.execute(
                f"SELECT canonical_name,entity_type,COUNT(*) AS mentions FROM {AI_DB}.v_ai_entity_timeline "
                "WHERE project_id=%s GROUP BY canonical_name,entity_type ORDER BY mentions DESC LIMIT 40",
                (project_id,),
            )
            entities = [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()
    return {
        "inventory": inventory,
        "available_event_types": event_types,
        "available_evidence_types": evidence_types,
        "fact_snapshot": facts,
        "risk_snapshot": risks,
        "entity_snapshot": entities,
    }


def _load_project_data(cursor, project_id: int) -> dict[str, Any]:
    cursor.execute(f"SELECT id,event_type,event_date,event_date_text,subject,action,object,amount,amount_unit,summary,status,confidence,need_review FROM {SOURCE_DB}.events WHERE project_id=%s ORDER BY id", (project_id,))
    events = list(cursor.fetchall())
    cursor.execute(f"SELECT e.id,e.evidence_type,e.title,e.excerpt,e.status,e.confidence,e.need_review,e.file_id,e.raw_content_id,f.file_name,r.content_type,r.locator FROM {SOURCE_DB}.evidences e JOIN {SOURCE_DB}.files f ON f.id=e.file_id JOIN {SOURCE_DB}.raw_contents r ON r.id=e.raw_content_id WHERE e.project_id=%s ORDER BY e.id", (project_id,))
    evidences = list(cursor.fetchall())
    cursor.execute(f"SELECT event_id,evidence_id,relation_type,relation_source FROM {AI_DB}.v_ai_event_all_evidences WHERE project_id=%s", (project_id,))
    relations = list(cursor.fetchall())
    cursor.execute(f"SELECT event_id,canonical_name,entity_type,entity_role FROM {AI_DB}.v_ai_entity_timeline WHERE project_id=%s", (project_id,))
    entities = list(cursor.fetchall())
    cursor.execute(f"SELECT id,fact_key,value_text,status,confidence,evidence_ids,event_ids,need_review FROM {SOURCE_DB}.current_facts WHERE project_id=%s ORDER BY id", (project_id,))
    facts = list(cursor.fetchall())
    cursor.execute(f"SELECT id,risk_type,risk_title,risk_summary,severity,recommendation,confidence,evidence_ids,event_ids,need_review FROM {SOURCE_DB}.risks WHERE project_id=%s ORDER BY id", (project_id,))
    risks = list(cursor.fetchall())
    return {"events": events, "evidences": evidences, "relations": relations, "entities": entities, "facts": facts, "risks": risks}


def _keyword_hits(text: str, keywords: list[str]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


def _score_event(event: dict[str, Any], config: dict[str, Any], relation_types: list[str]) -> tuple[int, dict[str, Any]]:
    text = clean_text(" ".join(str(event.get(key) or "") for key in ["subject", "action", "object", "summary"]))
    hits = _keyword_hits(text, config["keywords"])
    score = len(hits) * 2
    if event["event_type"] in config["event_types"]:
        score += 10
    if event.get("need_review"):
        score += 4
    if "conflicting" in relation_types:
        score += 6
    if "supporting" in relation_types:
        score += 2
    return score, {"keyword_hits": hits[:8], "relation_types": sorted(set(relation_types))}


def _score_evidence(evidence: dict[str, Any], config: dict[str, Any], linked_event_scores: list[int], relation_types: list[str]) -> tuple[int, dict[str, Any]]:
    text = clean_text(f"{evidence.get('title')} {evidence.get('excerpt')}")
    hits = _keyword_hits(text, config["keywords"])
    score = len(hits) * 2
    if evidence["evidence_type"] in config["evidence_types"]:
        score += 10
    if linked_event_scores:
        score += min(8, max(linked_event_scores) // 3)
    if evidence.get("need_review"):
        score += 4
    if "conflicting" in relation_types:
        score += 7
    if "supporting" in relation_types:
        score += 3
    return score, {"keyword_hits": hits[:8], "relation_types": sorted(set(relation_types))}


def _raw_content_sweep(cursor, project_id: int, keywords: list[str], limit: int) -> tuple[int, list[dict[str, Any]]]:
    conditions = " OR ".join(["r.text_content LIKE %s"] * len(keywords))
    params = [project_id, *[f"%{keyword}%" for keyword in keywords]]
    cursor.execute(f"SELECT COUNT(*) AS count FROM {SOURCE_DB}.raw_contents r WHERE r.project_id=%s AND ({conditions})", params)
    match_count = int(cursor.fetchone()["count"])
    cursor.execute(
        f"SELECT r.id,r.file_id,r.content_type,r.locator,r.text_content,f.file_name FROM {SOURCE_DB}.raw_contents r JOIN {SOURCE_DB}.files f ON f.id=r.file_id WHERE r.project_id=%s AND ({conditions}) ORDER BY r.need_review DESC,r.id LIMIT %s",
        [*params, limit * 5],
    )
    candidates = list(cursor.fetchall())
    scored = []
    for row in candidates:
        hits = _keyword_hits(clean_text(row.get("text_content")), keywords)
        scored.append((len(hits), row, hits))
    scored.sort(key=lambda item: (-item[0], int(item[1]["id"])))
    selected = []
    for score, row, hits in scored[:limit]:
        selected.append({**row, "score": score, "keyword_hits": hits[:8]})
    return match_count, selected


def _retrieval_config(agent_name: str, directive: dict[str, Any] | None) -> dict[str, Any]:
    base = AGENT_CONFIG[agent_name]
    config = {
        "task": base["task"],
        "event_types": set(base["event_types"]),
        "evidence_types": set(base["evidence_types"]),
        "keywords": list(base["keywords"]),
    }
    if not directive:
        return config

    allowed_event_types = set().union(*(item["event_types"] for item in AGENT_CONFIG.values()))
    allowed_evidence_types = set().union(*(item["evidence_types"] for item in AGENT_CONFIG.values()))
    config["event_types"].update(
        value for value in directive.get("event_types") or [] if value in allowed_event_types
    )
    config["evidence_types"].update(
        value for value in directive.get("evidence_types") or [] if value in allowed_evidence_types
    )
    dynamic_terms = [
        *(directive.get("keywords") or []),
        *(directive.get("focus_entities") or []),
    ]
    for value in dynamic_terms:
        keyword = re.sub(r"[%_\x00-\x1f]", "", str(value)).strip()
        if 1 < len(keyword) <= 40 and keyword not in config["keywords"]:
            config["keywords"].append(keyword)
        if len(config["keywords"]) >= 40:
            break
    objective = clean_text(directive.get("objective"), 1000)
    if objective:
        config["task"] = objective
    return config


def retrieve_agent_context(
    project_id: int,
    agent_name: str,
    max_events: int | None = None,
    max_evidences: int | None = None,
    max_raw_contents: int | None = None,
    env_path: Path = Path(".env"),
    retrieval_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if agent_name not in AGENT_CONFIG:
        raise ValueError(f"未知Agent：{agent_name}")
    config = _retrieval_config(agent_name, retrieval_plan)
    max_events = max_events or int((retrieval_plan or {}).get("max_events") or os.getenv("RAG_MAX_EVENTS", "40"))
    max_evidences = max_evidences or int((retrieval_plan or {}).get("max_evidences") or os.getenv("RAG_MAX_EVIDENCES", "50"))
    max_raw_contents = max_raw_contents or int((retrieval_plan or {}).get("max_raw_contents") or os.getenv("RAG_MAX_RAW_CONTENTS", "8"))
    max_events = min(max(1, max_events), 200)
    max_evidences = min(max(1, max_evidences), 250)
    max_raw_contents = min(max(1, max_raw_contents), 30)
    inventory = inventory_project(project_id, env_path)
    connection = connect(env_path)
    try:
        with connection.cursor() as cursor:
            data = _load_project_data(cursor, project_id)
            event_relations: dict[int, list[dict[str, Any]]] = defaultdict(list)
            evidence_relations: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for relation in data["relations"]:
                event_relations[int(relation["event_id"])].append(relation)
                evidence_relations[int(relation["evidence_id"])].append(relation)
            event_entities: dict[int, list[str]] = defaultdict(list)
            for row in data["entities"]:
                event_entities[int(row["event_id"])].append(f"{row['canonical_name']}({row['entity_role']})")

            ranked_events = []
            event_score_map: dict[int, int] = {}
            for event in data["events"]:
                relation_types = [row["relation_type"] for row in event_relations.get(int(event["id"]), [])]
                score, reasons = _score_event(event, config, relation_types)
                event_score_map[int(event["id"])] = score
                ranked_events.append((score, event, reasons))
            ranked_events.sort(key=lambda item: (-item[0], int(item[1]["id"])))
            selected_events = ranked_events[:max_events]
            selected_event_ids = {int(item[1]["id"]) for item in selected_events}

            ranked_evidences = []
            for evidence in data["evidences"]:
                links = evidence_relations.get(int(evidence["id"]), [])
                linked_scores = [event_score_map.get(int(row["event_id"]), 0) for row in links]
                relation_types = [row["relation_type"] for row in links]
                score, reasons = _score_evidence(evidence, config, linked_scores, relation_types)
                if any(int(row["event_id"]) in selected_event_ids for row in links):
                    score += 5
                    reasons["linked_selected_event"] = True
                ranked_evidences.append((score, evidence, reasons))
            ranked_evidences.sort(key=lambda item: (-item[0], int(item[1]["id"])))
            selected_evidences = ranked_evidences[:max_evidences]
            raw_match_count, selected_raw = _raw_content_sweep(cursor, project_id, config["keywords"], max_raw_contents)
    finally:
        connection.close()

    event_lines = []
    for score, event, reasons in selected_events:
        relation_refs = [f"{row['evidence_id']}:{row['relation_type']}:{row['relation_source']}" for row in event_relations.get(int(event["id"]), [])]
        entities = "、".join(event_entities.get(int(event["id"]), [])) or clean_text(event.get("subject"), 160)
        event_lines.append(
            f"- [事件#{event['id']} | score={score} | {event.get('event_date_text') or event.get('event_date') or '日期待核'} | {event['event_type']}] "
            f"主体={entities}；动作={clean_text(event.get('action'),80)}；对象={clean_text(event.get('object'),160)}；"
            f"摘要={clean_text(event.get('summary'),520)}；证据={'、'.join(relation_refs) or '-'}；召回依据={reasons}"
        )
    evidence_lines = []
    for score, evidence, reasons in selected_evidences:
        evidence_lines.append(
            f"- [证据#{evidence['id']} | score={score} | {evidence['evidence_type']}] {clean_text(evidence.get('title'),160)}："
            f"{clean_text(evidence.get('excerpt'),520)}；来源={clean_text(evidence.get('file_name'),140)}/{evidence.get('content_type')}/{evidence.get('locator')}；召回依据={reasons}"
        )
    raw_lines = []
    for row in selected_raw:
        raw_lines.append(
            f"- [原文#{row['id']} | score={row['score']} | {row['content_type']}] {clean_text(row.get('text_content'),900)}；"
            f"来源={clean_text(row.get('file_name'),140)}/{row.get('locator')}；关键词={row['keyword_hits']}"
        )
    fact_lines = [f"- [事实#{row['id']}] {row['fact_key']}：{clean_text(row.get('value_text'),700)}；状态={row['status']}；置信度={row.get('confidence')}；待审核={bool(row.get('need_review'))}" for row in data["facts"]]
    risk_lines = [f"- [风险#{row['id']} | {row['severity']}] {row['risk_title']}：{clean_text(row.get('risk_summary'),700)}；建议={clean_text(row.get('recommendation'),350)}；待审核={bool(row.get('need_review'))}" for row in data["risks"]]
    context = "\n".join([
        f"# {inventory['project_name']}｜{agent_name}动态全库检索",
        "",
        "## Master任务",
        f"- {config['task']}",
        "",
        "## 全部当前事实",
        *fact_lines,
        "",
        "## 全部风险",
        *risk_lines,
        "",
        "## 动态召回事件",
        *event_lines,
        "",
        "## 动态召回证据",
        *evidence_lines,
        "",
        "## 原文回查",
        *raw_lines,
    ])
    report = {
        "agent": agent_name,
        "task": config["task"],
        "inventory": inventory["counts"],
        "events_scanned": len(data["events"]),
        "events_selected": len(selected_events),
        "events_excluded_after_scoring": len(data["events"]) - len(selected_events),
        "evidences_scanned": len(data["evidences"]),
        "evidences_selected": len(selected_evidences),
        "evidences_excluded_after_scoring": len(data["evidences"]) - len(selected_evidences),
        "facts_scanned": len(data["facts"]),
        "risks_scanned": len(data["risks"]),
        "graph_relations_scanned": len(data["relations"]),
        "entity_relations_scanned": len(data["entities"]),
        "raw_contents_evaluated_by_database": inventory["counts"]["raw_contents"],
        "raw_keyword_matches": raw_match_count,
        "raw_contents_selected_for_model": len(selected_raw),
        "selected_event_ids": [int(item[1]["id"]) for item in selected_events],
        "selected_evidence_ids": [int(item[1]["id"]) for item in selected_evidences],
        "selected_raw_content_ids": [int(item["id"]) for item in selected_raw],
        "full_structured_scan_completed": True,
        "raw_scan_method": "MySQL全项目关键词条件扫描；命中内容相关性排序后进入模型。",
        "selection_method": "事件类型、证据类型、关键词、图关系、冲突、待审核状态综合评分。",
        "master_directive_applied": {
            "objective": config["task"],
            "event_types": sorted(config["event_types"]),
            "evidence_types": sorted(config["evidence_types"]),
            "keywords": config["keywords"],
            "must_verify": list((retrieval_plan or {}).get("must_verify") or []),
        },
    }
    return {"sections": {"动态Graph RAG上下文": context}, "report": report}


def combine_coverage(inventory: dict[str, Any], reports: list[dict[str, Any]]) -> dict[str, Any]:
    event_ids = set()
    evidence_ids = set()
    raw_ids = set()
    for report in reports:
        event_ids.update(report.get("selected_event_ids") or [])
        evidence_ids.update(report.get("selected_evidence_ids") or [])
        raw_ids.update(report.get("selected_raw_content_ids") or [])
    counts = inventory.get("counts") or {}
    return {
        "project_id": inventory.get("project_id"),
        "project_name": inventory.get("project_name"),
        "database_inventory": counts,
        "agents_completed": [report.get("agent") for report in reports if report],
        "structured_scan": {
            "events_total": counts.get("events", 0),
            "events_scanned_per_agent": {report.get("agent"): report.get("events_scanned") for report in reports},
            "unique_events_selected_for_model": len(event_ids),
            "evidences_total": counts.get("evidences", 0),
            "evidences_scanned_per_agent": {report.get("agent"): report.get("evidences_scanned") for report in reports},
            "unique_evidences_selected_for_model": len(evidence_ids),
            "facts_total": counts.get("current_facts", 0),
            "risks_total": counts.get("risks", 0),
        },
        "raw_content_scan": {
            "raw_contents_total": counts.get("raw_contents", 0),
            "scan_method": "每个Agent使用专业关键词对全项目raw_contents执行数据库条件扫描。",
            "unique_raw_contents_selected_for_model": len(raw_ids),
        },
        "interpretation": "全部结构化事件和证据均参与每个Agent的评分扫描；只有高相关记录进入模型上下文。原文采用全库关键词扫描和按需回查，不等于逐条送入模型。",
        "unprocessed_structured_records": 0,
    }
