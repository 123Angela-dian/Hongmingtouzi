from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class CloudDatabaseFrontendTest(unittest.TestCase):
    def test_cloud_runner_returns_generated_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "report.md"
            report_path.write_text("# 云数据库报告\n\n测试正文", encoding="utf-8")
            service_result = {
                "run_id": 21,
                "project_id": 7,
                "full_report_path": str(report_path),
            }
            with (
                patch("database_game.service.load_mandate", return_value="mandate") as load_mandate,
                patch("database_game.service.run_database_game", return_value=service_result) as run_game,
            ):
                result, report = app._run_cloud_database_game(7)

        load_mandate.assert_called_once_with(Path("config/database_game_mandate.example.json"))
        run_game.assert_called_once_with(
            env_path=Path(".env"),
            project_id=7,
            mandate="mandate",
        )
        self.assertEqual(result, service_result)
        self.assertIn("云数据库报告", report)

    def test_default_project_id_is_validated(self) -> None:
        with patch.dict("os.environ", {"DATABASE_GAME_DEFAULT_PROJECT_ID": "invalid"}):
            self.assertEqual(app._default_cloud_project_id(), 1)
        with patch.dict("os.environ", {"DATABASE_GAME_DEFAULT_PROJECT_ID": "9"}):
            self.assertEqual(app._default_cloud_project_id(), 9)


if __name__ == "__main__":
    unittest.main()
