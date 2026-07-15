from __future__ import annotations

import json
import os

from .llm_client import ChatMessage, CherryINClient, CherryINConfigError, extract_text
from .model_config import model_for_node
from .tracing import flush_traces, traceable_node


@traceable_node("cherryin_connectivity_test")
def run_connectivity_test() -> dict[str, str | bool]:
    profile = model_for_node("coarse_filter_node")
    result: dict[str, str | bool] = {
        "model": profile.model,
        "api_key_configured": bool(os.getenv(profile.key_env)),
        "base_url_configured": bool(os.getenv(profile.base_url_env)),
    }
    client = CherryINClient()
    response = client.chat(
        profile,
        [
            ChatMessage(role="system", content="You are a connectivity test. Reply with exactly: ok"),
            ChatMessage(role="user", content="Return ok."),
        ],
        max_tokens=16,
    )
    result["response_preview"] = extract_text(response)[:80]
    result["success"] = True
    return result


def main() -> None:
    try:
        result = run_connectivity_test()
    except CherryINConfigError as exc:
        result = {
            "success": False,
            "error": str(exc),
            "api_key_configured": bool(os.getenv("CHERRYIN_API_KEY")),
            "base_url_configured": bool(os.getenv("CHERRYIN_BASE_URL")),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    flush_traces()


if __name__ == "__main__":
    main()
