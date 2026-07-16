from __future__ import annotations

import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ModelProfile:
    provider: str
    model: str
    purpose: str
    key_env: str = "CHERRYIN_API_KEY"
    base_url_env: str = "CHERRYIN_BASE_URL"

    def as_trace_metadata(self) -> dict[str, str | bool]:
        return {
            **asdict(self),
            "api_key_configured": bool(os.getenv(self.key_env)),
            "base_url_configured": bool(os.getenv(self.base_url_env)),
        }


NODE_MODELS: dict[str, ModelProfile] = {
    "source_scan_node": ModelProfile(
        provider="cherryin",
        model="deepseek/deepseek-v4-pro",
        purpose="source parsing and raw notice normalization",
    ),
    "coarse_filter_node": ModelProfile(
        provider="cherryin",
        model="deepseek/deepseek-v4-pro",
        purpose="fast broad filtering",
    ),
    "dedupe_node_simple": ModelProfile(
        provider="cherryin",
        model="deepseek/deepseek-v4-pro",
        purpose="deterministic or easy duplicate checks",
    ),
    "dedupe_node_complex": ModelProfile(
        provider="cherryin",
        model="z-ai/glm-5.2",
        purpose="ambiguous duplicate and same-asset update judgment",
    ),
    "source_taxonomy_node": ModelProfile(
        provider="cherryin",
        model="z-ai/glm-5.2",
        purpose="dropdown/category business relevance classification",
    ),
    "detailed_screening_node": ModelProfile(
        provider="cherryin",
        model="openai/gpt-5.6-luna",
        purpose="high-stakes trackability scoring and rationale",
    ),
    "asset_pool_update_node": ModelProfile(
        provider="cherryin",
        model="openai/gpt-4o-mini",
        purpose="compact update summarization and field normalization",
    ),
    "detail_readability_node": ModelProfile(
        provider="cherryin",
        model="deepseek/deepseek-v4-pro",
        purpose="reader-friendly announcement detail restructuring",
    ),
    "asset_readability_reviewer_node": ModelProfile(
        provider="cherryin",
        model="z-ai/glm-5.2",
        purpose="asset pool readability and noise quality gate",
    ),
    "interest_alert_node": ModelProfile(
        provider="cherryin",
        model="deepseek/deepseek-v4-pro",
        purpose="watchlist alert generation",
    ),
    "report_generation": ModelProfile(
        provider="cherryin",
        model="anthropic/claude-opus-4.8",
        purpose="asset deep-dive analysis and report generation",
    ),
}


def model_for_node(node_name: str) -> ModelProfile:
    return NODE_MODELS[node_name]


def dedupe_model(complex_case: bool) -> ModelProfile:
    return NODE_MODELS["dedupe_node_complex" if complex_case else "dedupe_node_simple"]


def model_plan() -> dict[str, dict[str, str | bool]]:
    return {name: profile.as_trace_metadata() for name, profile in NODE_MODELS.items()}
