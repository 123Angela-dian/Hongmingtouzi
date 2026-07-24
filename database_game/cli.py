from __future__ import annotations

import argparse
import json
from pathlib import Path

from database_game.models import MasterMandate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="云数据库困境资产博弈测试引擎")
    parser.add_argument("command", choices=["init-schema", "validate-mandate", "run", "render-report"])
    parser.add_argument("--mandate", type=Path)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--project-id", type=int, default=1)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "init-schema":
        from database_game.service import initialize_game_schema

        result = initialize_game_schema(args.env)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "render-report":
        if args.run_id is None:
            raise SystemExit("render-report 命令必须提供 --run-id")
        from database_game.service import regenerate_complete_report

        result = regenerate_complete_report(
            env_path=args.env,
            run_id=args.run_id,
            output_path=args.output,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.mandate is None:
        raise SystemExit("validate-mandate 和 run 命令必须提供 --mandate")
    mandate = MasterMandate.model_validate_json(args.mandate.read_text(encoding="utf-8"))
    if args.command == "validate-mandate":
        result = {"valid": True, "mandate": mandate.model_dump(mode="json")}
    else:
        from database_game.service import run_database_game

        result = run_database_game(
            env_path=args.env,
            project_id=args.project_id,
            mandate=mandate,
            output_path=args.output,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
