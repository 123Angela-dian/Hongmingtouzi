from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError


ModelT = TypeVar("ModelT", bound=BaseModel)


class StructuredModelGateway(Protocol):
    def invoke(self, output_model: type[ModelT], system_prompt: str, user_payload: str) -> ModelT:
        ...


class BrainStructuredGateway:
    """Validate OpenAI-compatible model output against a Pydantic contract."""

    def __init__(self, max_attempts: int = 2):
        self.max_attempts = max(1, max_attempts)

    def invoke(self, output_model: type[ModelT], system_prompt: str, user_payload: str) -> ModelT:
        from workflow import _extract_json_object, _invoke_brain

        schema = json.dumps(output_model.model_json_schema(), ensure_ascii=False)
        validation_feedback = ""
        for attempt in range(1, self.max_attempts + 1):
            prompt = (
                f"{system_prompt.strip()}\n\n"
                "只输出严格 JSON，不要 Markdown，不要解释。输出必须满足以下 JSON Schema：\n"
                f"{schema}"
            )
            if validation_feedback:
                prompt += f"\n\n上一次输出校验失败，必须修正：{validation_feedback}"
            raw = _invoke_brain(prompt, user_payload)
            try:
                payload = _normalize_model_ranges(_extract_json_object(raw))
                return output_model.model_validate(payload)
            except (ValidationError, ValueError) as exc:
                validation_feedback = str(exc)[:4000]
                if attempt == self.max_attempts:
                    raise
        raise RuntimeError("structured model invocation exhausted")


def _normalize_model_ranges(value):
    """Repair mechanical range bounds and ordering in model output."""

    if isinstance(value, list):
        return [_normalize_model_ranges(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {key: _normalize_model_ranges(item) for key, item in value.items()}
    evidence_refs = normalized.get("evidence_refs")
    if isinstance(evidence_refs, list):
        normalized["evidence_refs"] = [
            item
            for item in evidence_refs
            if isinstance(item, dict) and item.get("source_table") and item.get("record_id")
        ]
        if normalized.get("kind") == "evidence_inference" and not normalized["evidence_refs"]:
            normalized["kind"] = "industry_prior"
    if {"minimum", "preferred", "maximum"}.issubset(normalized):
        try:
            minimum = Decimal(str(normalized["minimum"]))
            preferred = Decimal(str(normalized["preferred"]))
            maximum = Decimal(str(normalized["maximum"]))
        except (InvalidOperation, TypeError, ValueError):
            return normalized
        minimum = max(minimum, Decimal("0"))
        preferred = max(preferred, Decimal("0"))
        maximum = max(maximum, Decimal("0"))
        if normalized.get("unit") == "ratio":
            minimum = min(minimum, Decimal("1"))
            preferred = min(preferred, Decimal("1"))
            maximum = min(maximum, Decimal("1"))
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        preferred = min(max(preferred, minimum), maximum)
        normalized.update(
            {
                "minimum": str(minimum),
                "preferred": str(preferred),
                "maximum": str(maximum),
            }
        )
    return normalized
