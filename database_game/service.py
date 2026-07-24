from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from database_game.gateway import BrainStructuredGateway
from database_game.graph import DatabaseGameEngine
from database_game.models import CompleteAnalysisReport, DecisionMatrixReport, MasterMandate
from database_game.repository import CloudDatabaseRepository
from database_game.reporting import build_assumption_disclosures, render_complete_report
from database_game.prompts import COMPLETE_REPORT_PROMPT


AI_DB = "hongming_ai"
PROMPT_VERSION = "database-game-v2.2"


def load_mandate(path: Path) -> MasterMandate:
    return MasterMandate.model_validate_json(path.read_text(encoding="utf-8"))


def initialize_game_schema(env_path: Path) -> dict[str, Any]:
    from graph_rag import connect

    schema_path = Path(__file__).resolve().parent.parent / "sql" / "database_game_schema.sql"
    statements = [statement.strip() for statement in schema_path.read_text(encoding="utf-8").split(";") if statement.strip()]
    connection = connect(env_path)
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        connection.commit()
    finally:
        connection.close()
    return {"initialized": True, "schema": str(schema_path), "statements": len(statements)}


def run_database_game(
    *,
    env_path: Path,
    project_id: int,
    mandate: MasterMandate,
    output_path: Path | None = None,
) -> dict[str, Any]:
    from graph_rag import connect, inventory_project

    inventory = inventory_project(project_id, env_path)
    connection = connect(env_path)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {AI_DB}.ai_pipeline_runs "
                "(project_id,run_type,status,prompt_version,model_name,input_counts,parameters) "
                "VALUES (%s,'database_game_simulation','running',%s,%s,%s,%s)",
                (
                    project_id,
                    PROMPT_VERSION,
                    os.getenv("BRAIN_MODEL", ""),
                    _json(inventory.get("counts") or {}),
                    _json({"mandate": mandate.model_dump(mode="json")}),
                ),
            )
            run_id = int(cursor.lastrowid)
        connection.commit()
    finally:
        connection.close()

    try:
        engine = DatabaseGameEngine(
            BrainStructuredGateway(),
            repository=CloudDatabaseRepository(env_path),
        )
        state = engine.run_state(project_id, mandate)
        report = DecisionMatrixReport.model_validate(state["final_report"])
        complete_report = CompleteAnalysisReport.model_validate(state["complete_report"])
        complete_report_markdown = state["complete_report_markdown"]
        connection = connect(env_path)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {AI_DB}.ai_game_reports "
                    "(run_id,project_id,mandate,master_plan,public_context,scenario_parameters,deal_box,red_team_review,decision_report) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        run_id,
                        project_id,
                        _json(mandate.model_dump(mode="json")),
                        _json(state["master_plan"]),
                        _json(state["public_context"]),
                        _json(state["scenario_set"]),
                        _json(state["deal_box"]),
                        _json(state["red_team_review"]),
                        _json(report.model_dump(mode="json")),
                    ),
                )
                cursor.execute(
                    f"INSERT INTO {AI_DB}.ai_game_full_reports "
                    "(run_id,project_id,structured_report,markdown_report) VALUES (%s,%s,%s,%s)",
                    (
                        run_id,
                        project_id,
                        _json(complete_report.model_dump(mode="json")),
                        complete_report_markdown,
                    ),
                )
                cursor.execute(
                    f"UPDATE {AI_DB}.ai_pipeline_runs SET status='completed',finished_at=NOW() WHERE id=%s",
                    (run_id,),
                )
            connection.commit()
        finally:
            connection.close()
    except Exception as exc:
        connection = connect(env_path)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {AI_DB}.ai_pipeline_runs "
                    "SET status='failed',finished_at=NOW(),error_message=%s WHERE id=%s",
                    (str(exc)[:6000], run_id),
                )
            connection.commit()
        finally:
            connection.close()
        raise

    resolved_output = output_path or Path("output") / f"database_game_{project_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    full_json_output = resolved_output.with_name(f"{resolved_output.stem}_full.json")
    markdown_output = resolved_output.with_suffix(".md")
    full_json_output.write_text(
        json.dumps(complete_report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_output.write_text(complete_report_markdown, encoding="utf-8")
    return {
        "run_id": run_id,
        "project_id": project_id,
        "report_path": str(resolved_output),
        "full_report_json_path": str(full_json_output),
        "full_report_path": str(markdown_output),
    }


def regenerate_complete_report(
    *,
    env_path: Path,
    run_id: int,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Regenerate only the narrative layer from an already completed decision run."""

    from graph_rag import connect

    connection = connect(env_path)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT project_id,mandate,public_context,decision_report FROM {AI_DB}.ai_game_reports WHERE run_id=%s",
                (run_id,),
            )
            source = cursor.fetchone()
    finally:
        connection.close()
    if not source:
        raise ValueError(f"no completed database game report found for run_id={run_id}")

    project_id = int(source["project_id"])
    mandate = _decode_json(source["mandate"])
    public_context = _decode_json(source["public_context"])
    final_report = _decode_json(source["decision_report"])
    assumption_disclosures = build_assumption_disclosures(final_report)
    complete_report = BrainStructuredGateway().invoke(
        CompleteAnalysisReport,
        COMPLETE_REPORT_PROMPT,
        _json(
            {
                "public_context": public_context,
                "investor_mandate": mandate,
                "final_decision_report": final_report,
                "approved_simulated_assumptions": assumption_disclosures,
            }
        ),
    )
    complete_report = complete_report.model_copy(update={"simulated_assumptions": assumption_disclosures})
    markdown = render_complete_report(complete_report, public_context)

    connection = connect(env_path)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {AI_DB}.ai_game_full_reports "
                "(run_id,project_id,structured_report,markdown_report) VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE structured_report=VALUES(structured_report),markdown_report=VALUES(markdown_report)",
                (
                    run_id,
                    project_id,
                    _json(complete_report.model_dump(mode="json")),
                    markdown,
                ),
            )
        connection.commit()
    finally:
        connection.close()

    markdown_output = output_path or Path("output") / f"database_game_{project_id}_run_{run_id}.md"
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output = markdown_output.with_name(f"{markdown_output.stem}_full.json")
    markdown_output.write_text(markdown, encoding="utf-8")
    json_output.write_text(
        json.dumps(complete_report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "project_id": project_id,
        "full_report_json_path": str(json_output),
        "full_report_path": str(markdown_output),
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _decode_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)
