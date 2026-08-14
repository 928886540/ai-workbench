"""Curated model catalog exposed by the Leon CLI."""

from __future__ import annotations

MODEL_IDS = (
    "gemini-3.1-pro-preview",
    "gemini-2.5-pro-1m",
    "grok-4.5",
    "deepseek-v4-flash-0731",
    "glm-5.2",
    "kimi-k3",
    "gpt-5.6-luna",
    "grok-4.6",
    "deepseek-v4-pro-0813",
    "gpt-5.6-sol",
)


def resolve_model_id(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.isdigit():
        index = int(candidate) - 1
        return MODEL_IDS[index] if 0 <= index < len(MODEL_IDS) else None
    folded = candidate.casefold()
    known_model = next(
        (model_id for model_id in MODEL_IDS if model_id.casefold() == folded),
        None,
    )
    return known_model or candidate
