from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .model_config import ModelProfile


class CherryINConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


def _normalized_chat_url(base_url: str) -> str:
    cleaned = base_url.rstrip("/")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    if cleaned.endswith("/v1"):
        return f"{cleaned}/chat/completions"
    return f"{cleaned}/v1/chat/completions"


def _load_env_file() -> None:
    """Load .env first, then .env.example as a local fallback.

    This keeps all CherryIN-routed models on the same CHERRYIN_API_KEY and
    CHERRYIN_BASE_URL without requiring PowerShell env setup every time.
    Existing OS environment variables always win.
    """
    root = Path(__file__).resolve().parents[2]
    for name in (".env", ".env.example"):
        path = root / name
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ and value != "replace_me":
                os.environ[key] = value


class CherryINClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        _load_env_file()
        self.api_key = api_key or os.getenv("CHERRYIN_API_KEY")
        self.base_url = base_url or os.getenv("CHERRYIN_BASE_URL")
        if not self.api_key:
            raise CherryINConfigError("CHERRYIN_API_KEY is not configured")
        if not self.base_url:
            raise CherryINConfigError("CHERRYIN_BASE_URL is not configured")

    def chat(
        self,
        profile: ModelProfile,
        messages: list[ChatMessage],
        *,
        temperature: float = 0,
        max_tokens: int = 512,
        timeout: float = 60,
    ) -> dict[str, Any]:
        payload = {
            "model": profile.model,
            "messages": [message.__dict__ for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        response = httpx.post(
            _normalized_chat_url(self.base_url),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()


def extract_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    reasoning = message.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) else ""
