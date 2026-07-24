from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class GameRepository(Protocol):
    def planning_snapshot(self, project_id: int) -> dict[str, Any]:
        ...

    def retrieve_context(
        self,
        project_id: int,
        agent_name: str,
        retrieval_plan: dict[str, Any],
    ) -> dict[str, Any]:
        ...


class CloudDatabaseRepository:
    def __init__(self, env_path: Path = Path(".env")):
        self.env_path = env_path

    def planning_snapshot(self, project_id: int) -> dict[str, Any]:
        from graph_rag import load_planning_snapshot

        return load_planning_snapshot(project_id, self.env_path)

    def retrieve_context(
        self,
        project_id: int,
        agent_name: str,
        retrieval_plan: dict[str, Any],
    ) -> dict[str, Any]:
        from graph_rag import retrieve_agent_context

        return retrieve_agent_context(
            project_id,
            agent_name,
            env_path=self.env_path,
            retrieval_plan=retrieval_plan,
        )
