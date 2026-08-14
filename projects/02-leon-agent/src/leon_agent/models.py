"""Curated model catalog exposed by the Leon CLI."""

from __future__ import annotations

from collections.abc import Sequence


def resolve_model_id(value: str, model_ids: Sequence[str] = ()) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.isdigit():
        index = int(candidate) - 1
        return model_ids[index] if 0 <= index < len(model_ids) else None
    # Model ids are provider-defined and may be case-sensitive. Never normalize
    # a manually entered id; only numeric shortcuts resolve through the catalog.
    return candidate


def model_provider_scope(*, profile: str, base_url: str) -> str:
    """Bind a session model override to the exact provider endpoint that supplied it."""
    return f"{profile}|{base_url.strip().rstrip('/')}"
