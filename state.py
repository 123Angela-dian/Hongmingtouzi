from __future__ import annotations

from typing import Annotated, List, TypedDict


def append_list(existing: List[str] | None, incoming: List[str] | None) -> List[str]:
    """Reducer used by LangGraph when parallel nodes write patch lists."""
    return (existing or []) + (incoming or [])


class ProjectState(TypedDict, total=False):
    # 输入槽：前置硬分流后的四类标准文本数据
    equity_and_history_data: str
    financial_and_cost_data: str
    creditor_and_seizure_data: str
    asset_and_mortgage_data: str
    csv_evidence_rows: List[dict]
    metric_results: dict
    project_id: int
    project_name: str
    data_source: str
    parsed_artifact_key: str
    master_inventory: dict
    master_plan: dict
    asset_retrieval_report: dict
    economic_retrieval_report: dict
    legal_retrieval_report: dict
    financial_retrieval_report: dict
    coverage_report: dict

    # 中间诊断槽：并发节点只增量写入自己的 patches
    asset_patches: Annotated[List[str], append_list]
    economic_patches: Annotated[List[str], append_list]
    legal_patches: Annotated[List[str], append_list]
    financial_patches: Annotated[List[str], append_list]

    # 最终输出槽
    pcs_score: int
    pcs_breakdown: dict
    final_report: str


def empty_state() -> ProjectState:
    return {
        "equity_and_history_data": "",
        "financial_and_cost_data": "",
        "creditor_and_seizure_data": "",
        "asset_and_mortgage_data": "",
        "csv_evidence_rows": [],
        "metric_results": {},
        "project_id": 0,
        "project_name": "",
        "data_source": "",
        "parsed_artifact_key": "",
        "master_inventory": {},
        "master_plan": {},
        "asset_retrieval_report": {},
        "economic_retrieval_report": {},
        "legal_retrieval_report": {},
        "financial_retrieval_report": {},
        "coverage_report": {},
        "asset_patches": [],
        "economic_patches": [],
        "legal_patches": [],
        "financial_patches": [],
        "pcs_score": 0,
        "pcs_breakdown": {},
        "final_report": "",
    }
