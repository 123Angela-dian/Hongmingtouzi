from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

SOURCE_DB = "hongming01"
AI_DB = "hongming_ai"
PROMPT_VERSION = "hongming-ai-derived-v1"

EVENT_EVIDENCE_TYPES = {
    "equity_change": {"equity", "contract", "approval"},
    "claim_formation": {"claim", "contract", "financial_metric"},
    "claim_transfer": {"claim", "contract", "equity"},
    "repayment_or_offset": {"claim", "financial_metric", "contract"},
    "mortgage_or_pledge": {"mortgage", "pledge", "asset", "contract"},
    "seizure_or_litigation": {"legal_dispute", "claim", "asset", "mortgage"},
    "sales_update": {"sales", "financial_metric", "asset"},
    "valuation_or_projection": {"financial_metric", "asset", "cost", "sales"},
    "contract_signing_or_termination": {"contract", "claim", "legal_dispute"},
    "approval_or_transaction_plan": {"approval", "contract", "equity"},
    "project_operation": {"operation", "asset", "cost", "contract"},
}

EVENT_SECTIONS = {
    "equity_change": ("equity_and_history_data",),
    "claim_formation": ("creditor_and_seizure_data", "financial_and_cost_data"),
    "claim_transfer": ("creditor_and_seizure_data",),
    "repayment_or_offset": ("creditor_and_seizure_data", "financial_and_cost_data"),
    "mortgage_or_pledge": ("asset_and_mortgage_data", "creditor_and_seizure_data"),
    "seizure_or_litigation": ("creditor_and_seizure_data",),
    "sales_update": ("financial_and_cost_data",),
    "valuation_or_projection": ("financial_and_cost_data", "asset_and_mortgage_data"),
    "contract_signing_or_termination": ("creditor_and_seizure_data", "asset_and_mortgage_data"),
    "approval_or_transaction_plan": ("equity_and_history_data", "asset_and_mortgage_data"),
    "project_operation": ("equity_and_history_data", "asset_and_mortgage_data"),
}

EVIDENCE_SECTIONS = {
    "asset": ("asset_and_mortgage_data",),
    "mortgage": ("asset_and_mortgage_data", "creditor_and_seizure_data"),
    "pledge": ("asset_and_mortgage_data", "creditor_and_seizure_data"),
    "claim": ("creditor_and_seizure_data", "financial_and_cost_data"),
    "equity": ("equity_and_history_data",),
    "financial_metric": ("financial_and_cost_data",),
    "sales": ("financial_and_cost_data",),
    "cost": ("financial_and_cost_data",),
    "legal_dispute": ("creditor_and_seizure_data",),
    "approval": ("equity_and_history_data", "asset_and_mortgage_data"),
    "contract": ("creditor_and_seizure_data", "asset_and_mortgage_data"),
    "operation": ("equity_and_history_data", "asset_and_mortgage_data"),
    "risk_signal": ("creditor_and_seizure_data", "financial_and_cost_data"),
}

SECTION_TITLES = {
    "equity_and_history_data": "股权与历史沿革数据",
    "financial_and_cost_data": "货值与开发成本数据",
    "creditor_and_seizure_data": "金融机构债权与查封明细",
    "asset_and_mortgage_data": "资产明细与抵押物清册",
}

ENTITY_SYSTEM_PROMPT = """
你是困境资产尽调数据库的主体标准化引擎。根据事件记录提取真实法律或业务主体，并识别其在事件中的角色。
不得把金额、资产名称、合同名称、股权比例、普通事项描述当作主体。canonical_name 尽量使用材料中出现的最完整正式名称；无法确认简称对应完整名称时 need_review=true。
entity_type 只能使用 company/person/court/bank/trust/fund/government/other。source_field 只能使用 subject/object/summary。
role 使用简短中文，如项目公司、股东、转让方、受让方、债权人、债务人、抵押权人、抵押人、原告、被告、查封法院、合同方、审批机构、实际控制人。
只返回 JSON：{"items":[{"event_id":1,"entities":[{"mention":"原文名称","canonical_name":"标准名称","entity_type":"company","role":"项目公司","source_field":"subject","confidence":0.95,"need_review":false}]}]}
"""

EVIDENCE_SYSTEM_PROMPT = """
你是困境资产尽调的事件证据关系判断引擎。relation_type 只能是 supporting、conflicting、unrelated。
supporting 表示证据补充或佐证事件关键字段；conflicting 表示关键字段明确冲突；unrelated 表示只有泛化关键词相似。不得因为同属一个项目就判定 supporting。
只返回 JSON：{"items":[{"pair_id":"1:2","event_id":1,"evidence_id":2,"relation_type":"supporting","reason":"具体理由","confidence":0.92,"need_review":false}]}
"""


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(type(value).__name__)


def parse_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def clean_text(value: Any, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[: limit - 1] + "…" if limit and len(text) > limit else text


def normalized_name(value: str) -> str:
    text = clean_text(value).lower().replace("有限责任公司", "有限公司")
    return re.sub(r"[（）()【】\[\]《》“”‘’'\"·,，。；;：:\s]", "", text)


def database_config(env_path: Path) -> dict[str, Any]:
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


def connect(env_path: Path):
    import pymysql

    config = database_config(env_path)
    return pymysql.connect(
        host=config["host"], port=config["port"], user=config["user"], password=config["password"],
        charset="utf8mb4", connect_timeout=10, read_timeout=120, write_timeout=30, autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def invoke_brain(system_prompt: str, payload: Any) -> dict[str, Any]:
    from workflow import _extract_json_object, _invoke_brain

    result = _invoke_brain(system_prompt, json.dumps(payload, ensure_ascii=False, default=json_default))
    parsed = _extract_json_object(result)
    if not isinstance(parsed, dict):
        raise ValueError("模型未返回 JSON 对象")
    return parsed


def source_counts(cursor, project_id: int) -> dict[str, int]:
    counts = {}
    for table in ["files", "raw_contents", "evidences", "events", "event_evidences", "current_facts", "risks"]:
        cursor.execute(f"SELECT COUNT(*) AS count FROM {SOURCE_DB}.{table} WHERE project_id=%s", (project_id,))
        counts[table] = int(cursor.fetchone()["count"])
    return counts


def start_run(cursor, project_id: int, run_type: str, counts: dict[str, int], parameters: dict[str, Any]) -> int:
    cursor.execute(
        f"INSERT INTO {AI_DB}.ai_pipeline_runs (project_id,run_type,status,prompt_version,model_name,input_counts,parameters) VALUES (%s,%s,'running',%s,%s,%s,%s)",
        (project_id, run_type, PROMPT_VERSION, os.getenv("BRAIN_MODEL", ""), json.dumps(counts), json.dumps(parameters, ensure_ascii=False)),
    )
    return int(cursor.lastrowid)


def finish_run(cursor, run_id: int, status_value: str, error: str | None = None) -> None:
    cursor.execute(f"UPDATE {AI_DB}.ai_pipeline_runs SET status=%s,finished_at=NOW(),error_message=%s WHERE id=%s", (status_value, error, run_id))


def insert_review(cursor, project_id: int, review_type: str, target_table: str, target_id: int, issue_type: str, summary: str, priority: str, run_id: int) -> None:
    cursor.execute(
        f"INSERT INTO {AI_DB}.ai_review_queue (project_id,review_type,target_table,target_id,issue_type,issue_summary,priority,status,run_id) VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s) ON DUPLICATE KEY UPDATE issue_summary=VALUES(issue_summary),priority=VALUES(priority),run_id=VALUES(run_id)",
        (project_id, review_type, target_table, target_id, issue_type, summary, priority, run_id),
    )


def load_events(cursor, project_id: int) -> list[dict[str, Any]]:
    cursor.execute(f"SELECT id,event_type,event_date,event_date_text,subject,action,object,amount,amount_unit,summary,status,confidence,need_review FROM {SOURCE_DB}.events WHERE project_id=%s ORDER BY id", (project_id,))
    return list(cursor.fetchall())


def load_evidences(cursor, project_id: int) -> list[dict[str, Any]]:
    cursor.execute(
        f"SELECT e.id,e.evidence_type,e.title,e.excerpt,e.status,e.confidence,e.need_review,e.file_id,e.raw_content_id,f.file_name,r.content_type,r.locator FROM {SOURCE_DB}.evidences e JOIN {SOURCE_DB}.files f ON f.id=e.file_id JOIN {SOURCE_DB}.raw_contents r ON r.id=e.raw_content_id WHERE e.project_id=%s ORDER BY e.id",
        (project_id,),
    )
    return list(cursor.fetchall())


def ensure_entity(cursor, project_id: int, entity: dict[str, Any], run_id: int) -> int:
    canonical = clean_text(entity.get("canonical_name") or entity.get("mention"), 512)
    mention = clean_text(entity.get("mention") or canonical, 512)
    canonical_key = normalized_name(canonical)
    mention_key = normalized_name(mention)
    if not canonical_key:
        raise ValueError("主体名称为空")
    cursor.execute(f"SELECT entity_id FROM {AI_DB}.ai_entity_aliases WHERE project_id=%s AND normalized_alias=%s", (project_id, mention_key))
    alias_row = cursor.fetchone()
    if alias_row:
        return int(alias_row["entity_id"])
    cursor.execute(
        f"INSERT INTO {AI_DB}.ai_entities (project_id,canonical_name,normalized_name,entity_type,status,source_method,prompt_version,model_name,confidence,need_review,review_status,run_id) VALUES (%s,%s,%s,%s,%s,'rule_ai',%s,%s,%s,%s,'pending',%s) ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id),canonical_name=IF(CHAR_LENGTH(VALUES(canonical_name))>CHAR_LENGTH(canonical_name),VALUES(canonical_name),canonical_name),entity_type=IF(entity_type='unknown',VALUES(entity_type),entity_type),confidence=GREATEST(COALESCE(confidence,0),COALESCE(VALUES(confidence),0)),updated_at=NOW()",
        (project_id, canonical, canonical_key, entity.get("entity_type") or "unknown", "need_review" if entity.get("need_review") else "identified", PROMPT_VERSION, os.getenv("BRAIN_MODEL", ""), entity.get("confidence"), int(bool(entity.get("need_review"))), run_id),
    )
    entity_id = int(cursor.lastrowid)
    for alias_name, alias_key, alias_type in [(canonical, canonical_key, "canonical"), (mention, mention_key, "mention")]:
        cursor.execute(
            f"INSERT INTO {AI_DB}.ai_entity_aliases (project_id,entity_id,alias_name,normalized_alias,alias_type,source_event_id,source_method,confidence,need_review,review_status,run_id) VALUES (%s,%s,%s,%s,%s,%s,'rule_ai',%s,%s,'pending',%s) ON DUPLICATE KEY UPDATE entity_id=VALUES(entity_id),confidence=GREATEST(COALESCE(confidence,0),COALESCE(VALUES(confidence),0)),updated_at=NOW()",
            (project_id, entity_id, alias_name, alias_key, alias_type, entity.get("event_id"), entity.get("confidence"), int(bool(entity.get("need_review"))), run_id),
        )
    return entity_id


def infer_subject_role(event: dict[str, Any]) -> str:
    event_type = event["event_type"]
    action = clean_text(event.get("action"))
    if event_type == "equity_change":
        return "股权交易主体"
    if event_type in {"claim_formation", "claim_transfer", "repayment_or_offset"}:
        return "债权关系主体"
    if event_type == "mortgage_or_pledge":
        return "担保关系主体"
    if event_type == "seizure_or_litigation":
        return "诉讼或执行主体"
    if event_type == "approval_or_transaction_plan":
        return "审批或交易主体"
    if "成立" in action:
        return "项目公司"
    return "事件主体"


def rule_entity(event: dict[str, Any]) -> dict[str, Any] | None:
    subject = clean_text(event.get("subject"), 512)
    blocked = ["项目", "地块", "资金", "股东", "收购方", "权利人", "贷款", "合同", "协议", "保证人", "债权人", "债务人", "意向方", "甲方", "乙方", "双方", "各方", "住宅", "商服", "酒店"]
    if not subject or any(word in subject for word in blocked) or re.search(r"[/,，、;；]", subject):
        return None
    entity_type = None
    if "法院" in subject:
        entity_type = "court"
    elif "银行" in subject:
        entity_type = "bank"
    elif "信托" in subject:
        entity_type = "trust"
    elif any(word in subject for word in ["公司", "集团", "企业", "中心", "合伙"]):
        entity_type = "company"
    elif re.fullmatch(r"[\u4e00-\u9fff]{2,4}", subject):
        entity_type = "person"
    if not entity_type:
        return None
    return {
        "event_id": event["id"], "mention": subject, "canonical_name": subject,
        "entity_type": entity_type, "role": infer_subject_role(event), "source_field": "subject",
        "confidence": 0.82, "need_review": False,
    }


def enrich_entities(env_path: Path, project_id: int, batch_size: int, use_ai: bool) -> dict[str, int]:
    connection = connect(env_path)
    run_id = 0
    stats = {"events": 0, "entities": 0, "event_entities": 0, "review_items": 0, "ai_batches": 0}
    try:
        with connection.cursor() as cursor:
            events = load_events(cursor, project_id)
            run_id = start_run(cursor, project_id, "entity_enrichment", source_counts(cursor, project_id), {"batch_size": batch_size, "use_ai": use_ai})
            connection.commit()
            stats["events"] = len(events)
            for start in range(0, len(events), batch_size):
                batch = events[start:start + batch_size]
                extracted: dict[int, list[dict[str, Any]]] = defaultdict(list)
                if use_ai:
                    payload = {"events": [{"event_id": event["id"], "event_type": event["event_type"], "date": event.get("event_date_text") or event.get("event_date"), "subject": event.get("subject"), "action": event.get("action"), "object": event.get("object"), "summary": clean_text(event.get("summary"), 600)} for event in batch]}
                    try:
                        response = invoke_brain(ENTITY_SYSTEM_PROMPT, payload)
                        stats["ai_batches"] += 1
                        for item in response.get("items", []):
                            event_id = int(item.get("event_id"))
                            for entity in item.get("entities", []):
                                entity["event_id"] = event_id
                                extracted[event_id].append(entity)
                    except Exception as exc:
                        print(f"主体 AI 批次失败，使用规则回退：{start}-{start + len(batch)} | {exc}")
                for event in batch:
                    entities = extracted.get(int(event["id"])) or []
                    fallback = rule_entity(event)
                    if fallback and not any(normalized_name(item.get("mention", "")) == normalized_name(fallback["mention"]) for item in entities):
                        entities.append(fallback)
                    for entity in entities:
                        if not clean_text(entity.get("canonical_name") or entity.get("mention")):
                            continue
                        entity["event_id"] = event["id"]
                        entity_id = ensure_entity(cursor, project_id, entity, run_id)
                        cursor.execute(
                            f"INSERT INTO {AI_DB}.ai_event_entities (project_id,event_id,entity_id,entity_role,source_field,source_text,source_method,confidence,need_review,review_status,run_id) VALUES (%s,%s,%s,%s,%s,%s,'rule_ai',%s,%s,'pending',%s) ON DUPLICATE KEY UPDATE confidence=GREATEST(COALESCE(confidence,0),COALESCE(VALUES(confidence),0)),need_review=VALUES(need_review),run_id=VALUES(run_id),updated_at=NOW()",
                            (project_id, event["id"], entity_id, clean_text(entity.get("role") or "事件主体", 64), entity.get("source_field") if entity.get("source_field") in {"subject", "object", "summary"} else "summary", clean_text(entity.get("mention"), 512), entity.get("confidence"), int(bool(entity.get("need_review"))), run_id),
                        )
                        stats["event_entities"] += 1
                        if entity.get("need_review"):
                            insert_review(cursor, project_id, "entity", "ai_event_entities", int(cursor.lastrowid or 0), "entity_identity", f"主体需要确认：{entity.get('mention')} → {entity.get('canonical_name')}", "medium", run_id)
                            stats["review_items"] += 1
                connection.commit()
                print(f"主体识别进度：{min(start + batch_size, len(events))}/{len(events)}")
            cursor.execute(f"SELECT COUNT(*) AS count FROM {AI_DB}.ai_entities WHERE project_id=%s", (project_id,))
            stats["entities"] = int(cursor.fetchone()["count"])
            finish_run(cursor, run_id, "completed")
            connection.commit()
            return stats
    except Exception as exc:
        connection.rollback()
        if run_id:
            with connection.cursor() as cursor:
                finish_run(cursor, run_id, "failed", str(exc))
            connection.commit()
        raise
    finally:
        connection.close()


def significant_terms(event: dict[str, Any]) -> set[str]:
    terms = set(re.findall(r"\d+(?:\.\d+)?", " ".join(clean_text(event.get(key)) for key in ["subject", "action", "object", "summary"])))
    for value in [event.get("subject"), event.get("object")]:
        cleaned = clean_text(value)
        if 3 <= len(cleaned) <= 80:
            terms.add(cleaned)
        terms.update(re.findall(r"[\u4e00-\u9fffA-Za-z]{4,24}", cleaned))
    action = clean_text(event.get("action"))
    if len(action) >= 2:
        terms.add(action)
    return {term for term in terms if term}


def candidate_score(event: dict[str, Any], evidence: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    text = clean_text(f"{evidence.get('title')} {evidence.get('excerpt')}")
    score = 0
    reasons: dict[str, Any] = {}
    if evidence["evidence_type"] in EVENT_EVIDENCE_TYPES.get(event["event_type"], set()):
        score += 2
        reasons["compatible_type"] = True
    matched_terms = [term for term in significant_terms(event) if term in text]
    for term in matched_terms:
        score += 4 if re.fullmatch(r"\d+(?:\.\d+)?", term) else 3
    if matched_terms:
        reasons["matched_terms"] = matched_terms[:8]
    subject = clean_text(event.get("subject"))
    if subject and subject in text:
        score += 5
        reasons["same_subject"] = True
    amount = event.get("amount")
    if amount is not None and any(item in text for item in {str(amount), f"{float(amount):g}"}):
        score += 5
        reasons["same_amount"] = True
    return score, reasons


def evidence_candidates(events: list[dict[str, Any]], evidences: list[dict[str, Any]], existing: set[tuple[int, int]], per_event: int, max_pairs: int) -> list[dict[str, Any]]:
    pairs = []
    for event in events:
        ranked = []
        for evidence in evidences:
            if (int(event["id"]), int(evidence["id"])) in existing:
                continue
            score, reasons = candidate_score(event, evidence)
            if score >= 7:
                ranked.append((score, evidence, reasons))
        ranked.sort(key=lambda item: item[0], reverse=True)
        for score, evidence, reasons in ranked[:per_event]:
            pairs.append({"event": event, "evidence": evidence, "rule_score": score, "match_reasons": reasons})
    pairs.sort(key=lambda item: item["rule_score"], reverse=True)
    return pairs[:max_pairs] if max_pairs > 0 else pairs


def enrich_event_evidences(env_path: Path, project_id: int, batch_size: int, per_event: int, max_pairs: int) -> dict[str, int]:
    connection = connect(env_path)
    run_id = 0
    stats = {"candidate_pairs": 0, "supporting": 0, "conflicting": 0, "unrelated": 0, "review_items": 0, "ai_batches": 0}
    try:
        with connection.cursor() as cursor:
            events = load_events(cursor, project_id)
            evidences = load_evidences(cursor, project_id)
            cursor.execute(f"SELECT event_id,evidence_id FROM {SOURCE_DB}.event_evidences WHERE project_id=%s", (project_id,))
            existing = {(int(row["event_id"]), int(row["evidence_id"])) for row in cursor.fetchall()}
            cursor.execute(f"SELECT event_id,evidence_id FROM {AI_DB}.ai_event_evidences WHERE project_id=%s", (project_id,))
            existing.update((int(row["event_id"]), int(row["evidence_id"])) for row in cursor.fetchall())
            pairs = evidence_candidates(events, evidences, existing, per_event, max_pairs)
            stats["candidate_pairs"] = len(pairs)
            run_id = start_run(cursor, project_id, "event_evidence_enrichment", source_counts(cursor, project_id), {"batch_size": batch_size, "per_event": per_event, "max_pairs": max_pairs})
            connection.commit()
            for start in range(0, len(pairs), batch_size):
                batch = pairs[start:start + batch_size]
                payload = {"pairs": [{"pair_id": f"{item['event']['id']}:{item['evidence']['id']}", "event": {"event_id": item["event"]["id"], "event_type": item["event"]["event_type"], "date": item["event"].get("event_date_text") or item["event"].get("event_date"), "subject": item["event"].get("subject"), "action": item["event"].get("action"), "object": item["event"].get("object"), "amount": item["event"].get("amount"), "summary": clean_text(item["event"].get("summary"), 500)}, "evidence": {"evidence_id": item["evidence"]["id"], "evidence_type": item["evidence"]["evidence_type"], "title": clean_text(item["evidence"].get("title"), 180), "excerpt": clean_text(item["evidence"].get("excerpt"), 650), "file_name": clean_text(item["evidence"].get("file_name"), 160)}, "rule_match": item["match_reasons"]} for item in batch]}
                response = invoke_brain(EVIDENCE_SYSTEM_PROMPT, payload)
                stats["ai_batches"] += 1
                pair_map = {f"{item['event']['id']}:{item['evidence']['id']}": item for item in batch}
                for result in response.get("items", []):
                    pair_id = str(result.get("pair_id") or f"{result.get('event_id')}:{result.get('evidence_id')}")
                    pair = pair_map.get(pair_id)
                    relation_type = result.get("relation_type")
                    if not pair or relation_type not in {"supporting", "conflicting", "unrelated"}:
                        continue
                    stats[relation_type] += 1
                    if relation_type == "unrelated":
                        continue
                    event_id = int(pair["event"]["id"])
                    evidence_id = int(pair["evidence"]["id"])
                    confidence = float(result.get("confidence") or 0)
                    need_review = bool(result.get("need_review")) or confidence < 0.8 or relation_type == "conflicting"
                    reasons = dict(pair["match_reasons"])
                    reasons["ai_reason"] = clean_text(result.get("reason"), 800)
                    cursor.execute(
                        f"INSERT INTO {AI_DB}.ai_event_evidences (project_id,event_id,evidence_id,relation_type,relation_note,match_reasons,source_method,prompt_version,model_name,confidence,need_review,review_status,run_id) VALUES (%s,%s,%s,%s,%s,%s,'rule_ai',%s,%s,%s,%s,'pending',%s) ON DUPLICATE KEY UPDATE relation_type=VALUES(relation_type),relation_note=VALUES(relation_note),match_reasons=VALUES(match_reasons),confidence=VALUES(confidence),need_review=VALUES(need_review),run_id=VALUES(run_id),updated_at=NOW()",
                        (project_id, event_id, evidence_id, relation_type, clean_text(result.get("reason"), 2000), json.dumps(reasons, ensure_ascii=False), PROMPT_VERSION, os.getenv("BRAIN_MODEL", ""), confidence, int(need_review), run_id),
                    )
                    if need_review:
                        insert_review(cursor, project_id, "event_evidence", "ai_event_evidences", int(cursor.lastrowid or 0), "evidence_relation", f"事件 {event_id} 与证据 {evidence_id}：{relation_type}；{result.get('reason')}", "high" if relation_type == "conflicting" else "medium", run_id)
                        stats["review_items"] += 1
                connection.commit()
                print(f"证据关系识别进度：{min(start + batch_size, len(pairs))}/{len(pairs)}")
            finish_run(cursor, run_id, "completed")
            connection.commit()
            return stats
    except Exception as exc:
        connection.rollback()
        if run_id:
            with connection.cursor() as cursor:
                finish_run(cursor, run_id, "failed", str(exc))
            connection.commit()
        raise
    finally:
        connection.close()


def seed_source_reviews(env_path: Path, project_id: int) -> dict[str, int]:
    connection = connect(env_path)
    stats = defaultdict(int)
    run_id = 0
    try:
        with connection.cursor() as cursor:
            run_id = start_run(cursor, project_id, "source_review_seed", source_counts(cursor, project_id), {})
            for table, label, priority in [("evidences", "源证据待审核", "medium"), ("events", "源事件待审核", "medium"), ("current_facts", "当前事实待审核", "high"), ("risks", "风险结论待审核", "high")]:
                cursor.execute(f"SELECT id FROM {SOURCE_DB}.{table} WHERE project_id=%s AND need_review=1", (project_id,))
                for row in cursor.fetchall():
                    insert_review(cursor, project_id, "source_data", f"{SOURCE_DB}.{table}", int(row["id"]), "source_need_review", label, priority, run_id)
                    stats[table] += 1
            finish_run(cursor, run_id, "completed")
            connection.commit()
            return dict(stats)
    except Exception as exc:
        connection.rollback()
        if run_id:
            with connection.cursor() as cursor:
                finish_run(cursor, run_id, "failed", str(exc))
            connection.commit()
        raise
    finally:
        connection.close()



def consolidate_aliases(env_path: Path, project_id: int) -> dict[str, int]:
    connection = connect(env_path)
    stats = {"merged": 0, "ambiguous": 0, "unchanged": 0}
    run_id = 0
    try:
        with connection.cursor() as cursor:
            run_id = start_run(cursor, project_id, "alias_consolidation", source_counts(cursor, project_id), {})
            cursor.execute(f"SELECT id,canonical_name,normalized_name,entity_type FROM {AI_DB}.ai_entities WHERE project_id=%s ORDER BY CHAR_LENGTH(normalized_name) DESC", (project_id,))
            entities = list(cursor.fetchall())
            full_entities = [row for row in entities if any(word in row["canonical_name"] for word in ["有限公司", "集团", "银行", "信托", "法院", "合伙企业", "委员会", "政府"])]
            for source in sorted(entities, key=lambda row: len(row["normalized_name"])):
                if source in full_entities or len(source["normalized_name"]) < 2:
                    stats["unchanged"] += 1
                    continue
                matches = [target for target in full_entities if source["normalized_name"] in target["normalized_name"] and source["id"] != target["id"]]
                if len(matches) == 1:
                    target = matches[0]
                    cursor.execute(
                        f"INSERT IGNORE INTO {AI_DB}.ai_event_entities (project_id,event_id,entity_id,entity_role,source_field,source_text,source_method,confidence,need_review,review_status,run_id) SELECT project_id,event_id,%s,entity_role,source_field,source_text,source_method,confidence,need_review,review_status,%s FROM {AI_DB}.ai_event_entities WHERE entity_id=%s",
                        (target["id"], run_id, source["id"]),
                    )
                    cursor.execute(f"DELETE FROM {AI_DB}.ai_event_entities WHERE entity_id=%s", (source["id"],))
                    cursor.execute(f"UPDATE {AI_DB}.ai_entity_aliases SET entity_id=%s,run_id=%s WHERE entity_id=%s", (target["id"], run_id, source["id"]))
                    cursor.execute(f"DELETE FROM {AI_DB}.ai_entities WHERE id=%s", (source["id"],))
                    stats["merged"] += 1
                elif len(matches) > 1:
                    insert_review(cursor, project_id, "entity_alias", "ai_entities", int(source["id"]), "ambiguous_alias", f"简称 {source['canonical_name']} 可能对应：{'、'.join(item['canonical_name'] for item in matches)}", "medium", run_id)
                    stats["ambiguous"] += 1
                else:
                    stats["unchanged"] += 1
            finish_run(cursor, run_id, "completed")
            connection.commit()
            return stats
    except Exception as exc:
        connection.rollback()
        if run_id:
            with connection.cursor() as cursor:
                finish_run(cursor, run_id, "failed", str(exc))
            connection.commit()
        raise
    finally:
        connection.close()

def build_state(env_path: Path, project_id: int, max_events: int, max_evidences: int):
    from state import empty_state

    connection = connect(env_path)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT project_name FROM {SOURCE_DB}.projects WHERE id=%s", (project_id,))
            project = cursor.fetchone()
            if not project:
                raise ValueError(f"项目不存在：projects.id={project_id}")
            events = load_events(cursor, project_id)
            evidences = load_evidences(cursor, project_id)
            cursor.execute(f"SELECT event_id,evidence_id,relation_type,relation_source FROM {AI_DB}.v_ai_event_all_evidences WHERE project_id=%s", (project_id,))
            relations = list(cursor.fetchall())
            cursor.execute(f"SELECT fact_key,value_text,status,confidence,evidence_ids,event_ids FROM {SOURCE_DB}.current_facts WHERE project_id=%s ORDER BY id", (project_id,))
            facts = list(cursor.fetchall())
            cursor.execute(f"SELECT risk_type,risk_title,risk_summary,severity,recommendation,confidence,evidence_ids,event_ids FROM {SOURCE_DB}.risks WHERE project_id=%s ORDER BY id", (project_id,))
            risks = list(cursor.fetchall())
            cursor.execute(f"SELECT event_id,canonical_name,entity_type,entity_role FROM {AI_DB}.v_ai_entity_timeline WHERE project_id=%s", (project_id,))
            entity_rows = list(cursor.fetchall())
    finally:
        connection.close()

    event_evidence_ids = defaultdict(list)
    for row in relations:
        event_evidence_ids[int(row["event_id"])].append(f"{row['evidence_id']}:{row['relation_type']}:{row['relation_source']}")
    event_entities = defaultdict(list)
    for row in entity_rows:
        event_entities[int(row["event_id"])].append(f"{row['canonical_name']}({row['entity_role']})")
    section_events = defaultdict(list)
    section_evidences = defaultdict(list)
    for event in events:
        for section in EVENT_SECTIONS.get(event["event_type"], tuple(SECTION_TITLES)):
            section_events[section].append(event)
    for evidence in evidences:
        for section in EVIDENCE_SECTIONS.get(evidence["evidence_type"], tuple(SECTION_TITLES)):
            section_evidences[section].append(evidence)

    state = empty_state()
    risk_lines = [f"- [{row['severity']}] {row['risk_title']}：{clean_text(row['risk_summary'],700)}；建议={clean_text(row['recommendation'],350)}" for row in risks]
    fact_lines = [f"- {row['fact_key']}：{clean_text(row['value_text'],700)}；状态={row['status']}；置信度={row['confidence']}；证据={row['evidence_ids']}" for row in facts]
    for section, title in SECTION_TITLES.items():
        selected_events = section_events[section][:max_events] if max_events > 0 else section_events[section]
        selected_evidences = section_evidences[section][:max_evidences] if max_evidences > 0 else section_evidences[section]
        lines = [f"# {project['project_name']}｜{title}", "", "## 当前事实", *fact_lines, "", "## 已识别风险", *risk_lines, "", "## 时间事件"]
        for event in selected_events:
            entities = "、".join(event_entities.get(int(event["id"]), [])) or clean_text(event.get("subject"), 180)
            evidence_refs = "、".join(event_evidence_ids.get(int(event["id"]), [])) or "-"
            lines.append(f"- [事件#{event['id']} | {event.get('event_date_text') or event.get('event_date') or '日期待核'} | {event['event_type']}] 主体={entities}；动作={clean_text(event.get('action'),80)}；对象={clean_text(event.get('object'),180)}；摘要={clean_text(event.get('summary'),500)}；证据={evidence_refs}")
        lines.extend(["", "## 证据明细"])
        for evidence in selected_evidences:
            lines.append(f"- [证据#{evidence['id']} | {evidence['evidence_type']}] {clean_text(evidence['title'],160)}：{clean_text(evidence['excerpt'],500)}；来源={clean_text(evidence['file_name'],140)}/{evidence.get('content_type')}/{evidence.get('locator')}")
        state[section] = "\n".join(lines)
    state["csv_evidence_rows"] = [{"file": row["file_name"], "sheet": row.get("content_type") or "database", "row_index": row["raw_content_id"], "column": row["title"], "text": row["excerpt"], "category": EVIDENCE_SECTIONS.get(row["evidence_type"], ("equity_and_history_data",))[0], "date_version": "", "evidence_id": row["id"], "evidence_type": row["evidence_type"], "locator": parse_json(row.get("locator"))} for row in evidences]
    state["metric_results"] = {}
    state["project_id"] = project_id
    state["project_name"] = project["project_name"]
    state["data_source"] = "cloud_mysql_ai"
    return state


def run_workflow(env_path: Path, project_id: int, max_events: int, max_evidences: int, output: Path | None) -> dict[str, Any]:
    from workflow import compiled_graph

    state = build_state(env_path, project_id, max_events, max_evidences)
    connection = connect(env_path)
    run_id = 0
    try:
        with connection.cursor() as cursor:
            run_id = start_run(cursor, project_id, "langgraph_full_run", source_counts(cursor, project_id), {"max_events": max_events, "max_evidences": max_evidences})
            connection.commit()
        trace_state = dict(state)
        trace_state["csv_evidence_rows"] = []
        trace_state["metric_results"] = {}
        final_state = compiled_graph.invoke(trace_state, config={"run_name": "master_agent", "metadata": {"project_id": project_id, "source": "cloud_mysql_ai", "trace_payload": "bounded"}})
        final_state["csv_evidence_rows"] = state.get("csv_evidence_rows", [])
        final_state["metric_results"] = state.get("metric_results", {})
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {AI_DB}.ai_reports (run_id,project_id,pcs_score,pcs_breakdown,asset_patches,economic_patches,legal_patches,financial_patches,master_plan,coverage_report,final_report) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (run_id, project_id, final_state.get("pcs_score"), json.dumps(final_state.get("pcs_breakdown") or {}, ensure_ascii=False), json.dumps(final_state.get("asset_patches") or [], ensure_ascii=False), json.dumps(final_state.get("economic_patches") or [], ensure_ascii=False), json.dumps(final_state.get("legal_patches") or [], ensure_ascii=False), json.dumps(final_state.get("financial_patches") or [], ensure_ascii=False), json.dumps(final_state.get("master_plan") or {}, ensure_ascii=False), json.dumps(final_state.get("coverage_report") or {}, ensure_ascii=False), final_state.get("final_report") or ""),
            )
            finish_run(cursor, run_id, "completed")
            connection.commit()
        output_path = output or Path("output") / f"db_ai_run_{project_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(final_state, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
        return {"run_id": run_id, "report_path": str(output_path), "pcs_score": final_state.get("pcs_score")}
    except Exception as exc:
        connection.rollback()
        if run_id:
            with connection.cursor() as cursor:
                finish_run(cursor, run_id, "failed", str(exc))
            connection.commit()
        raise
    finally:
        connection.close()


def status(env_path: Path, project_id: int) -> dict[str, Any]:
    connection = connect(env_path)
    try:
        with connection.cursor() as cursor:
            result = {"project_id": project_id, "source": source_counts(cursor, project_id), "ai": {}}
            for table in ["ai_entities", "ai_entity_aliases", "ai_event_entities", "ai_event_evidences", "ai_review_queue", "ai_pipeline_runs", "ai_reports"]:
                cursor.execute(f"SELECT COUNT(*) AS count FROM {AI_DB}.{table} WHERE project_id=%s", (project_id,))
                result["ai"][table] = int(cursor.fetchone()["count"])
            cursor.execute(f"SELECT relation_type,COUNT(*) AS count FROM {AI_DB}.ai_event_evidences WHERE project_id=%s GROUP BY relation_type", (project_id,))
            result["ai_event_evidence_types"] = {row["relation_type"]: int(row["count"]) for row in cursor.fetchall()}
            return result
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="弘明项目云数据库 AI 派生层与现有 LangGraph 接入工具")
    parser.add_argument("command", choices=["seed-reviews", "enrich-entities", "consolidate-aliases", "enrich-evidences", "build-state", "run-workflow", "status"])
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--project-id", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--per-event", type=int, default=2)
    parser.add_argument("--max-pairs", type=int, default=300)
    parser.add_argument("--max-events", type=int, default=180)
    parser.add_argument("--max-evidences", type=int, default=180)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "seed-reviews":
        result = seed_source_reviews(args.env, args.project_id)
    elif args.command == "enrich-entities":
        result = enrich_entities(args.env, args.project_id, args.batch_size, not args.no_ai)
    elif args.command == "consolidate-aliases":
        result = consolidate_aliases(args.env, args.project_id)
    elif args.command == "enrich-evidences":
        result = enrich_event_evidences(args.env, args.project_id, args.batch_size, args.per_event, args.max_pairs)
    elif args.command == "build-state":
        state = build_state(args.env, args.project_id, args.max_events, args.max_evidences)
        output = args.output or Path("output") / f"db_ai_state_{args.project_id}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
        result = {"output": str(output), "evidence_rows": len(state.get("csv_evidence_rows") or []), "section_chars": {key: len(state.get(key, "")) for key in SECTION_TITLES}}
    elif args.command == "run-workflow":
        result = run_workflow(args.env, args.project_id, args.max_events, args.max_evidences, args.output)
    else:
        result = status(args.env, args.project_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=json_default))


if __name__ == "__main__":
    main()
