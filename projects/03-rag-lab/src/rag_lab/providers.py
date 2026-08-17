"""Offline and explicitly enabled live embedding providers."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from openai import OpenAI
from workbench_core.config import Settings

from rag_lab.embeddings import OpenAIEmbeddingProvider
from rag_lab.reranking import SiliconFlowReranker

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]", re.IGNORECASE)


class DeterministicFakeEmbeddingProvider:
    """Small lexical hash used to exercise the pipeline without API calls."""

    def __init__(self, *, dimensions: int = 256) -> None:
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 8:
            raise ValueError("dimensions must be an integer of at least 8")
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            normalized = str(text).casefold()
            tokens = _TOKEN_PATTERN.findall(normalized)
            chinese = [token for token in tokens if len(token) == 1 and token >= "\u3400"]
            tokens.extend(
                left + right
                for left, right in zip(chinese, chinese[1:], strict=False)
            )
            vector = [0.0] * self.dimensions
            for token in tokens or [normalized]:
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                vector[int.from_bytes(digest, "big") % self.dimensions] += 1.0
            vectors.append(vector)
        return vectors


@dataclass(frozen=True, slots=True)
class LiveEmbeddingConfig:
    model: str
    base_url: str
    api_key: str = field(repr=False)
    profile: str = "env"


@dataclass(frozen=True, slots=True)
class LiveRerankerConfig:
    model: str
    base_url: str
    api_key: str = field(repr=False)
    profile: str = "env"


def resolve_live_embedding_config(
    *,
    model: str | None = None,
    environ: Mapping[str, str] | None = None,
    shared_settings: Settings | None = None,
) -> LiveEmbeddingConfig:
    """Resolve live credentials only after the CLI has received explicit opt-in.

    The embedding model never falls back to the configured chat model. Base URL
    and key may be supplied as a dedicated pair, otherwise the shared lab provider
    is reused. Leon's private configuration is never consulted.
    """

    values = os.environ if environ is None else environ
    selected_model = str(model or values.get("RAG_EMBEDDING_MODEL") or "").strip()
    if not selected_model:
        raise ValueError(
            "embedding model is required; pass --model or set RAG_EMBEDDING_MODEL"
        )

    base_url = str(values.get("RAG_EMBEDDING_BASE_URL") or "").strip().rstrip("/")
    api_key = str(values.get("RAG_EMBEDDING_API_KEY") or "").strip()
    if bool(base_url) != bool(api_key):
        raise ValueError(
            "RAG_EMBEDDING_BASE_URL and RAG_EMBEDDING_API_KEY must be set together"
        )
    if base_url and api_key:
        return LiveEmbeddingConfig(
            model=selected_model,
            base_url=base_url,
            api_key=api_key,
            profile="rag-env",
        )

    settings = shared_settings or Settings()
    return LiveEmbeddingConfig(
        model=selected_model,
        base_url=settings.active_base_url,
        api_key=settings.require_api_key(),
        profile=f"shared:{settings.profile}",
    )


def build_live_embedding_provider(
    config: LiveEmbeddingConfig,
    *,
    timeout_seconds: float = 30.0,
    client_factory: Any = OpenAI,
) -> tuple[OpenAIEmbeddingProvider, Any]:
    """Build a closeable OpenAI-compatible client and its narrow adapter."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    client = client_factory(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=timeout_seconds,
        max_retries=0,
    )
    return OpenAIEmbeddingProvider(client, model=config.model), client


def resolve_live_reranker_config(
    *,
    model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> LiveRerankerConfig:
    """Resolve a dedicated reranker, reusing the configured embedding transport."""

    values = os.environ if environ is None else environ
    selected_model = str(model or values.get("RAG_RERANKER_MODEL") or "").strip()
    if not selected_model:
        raise ValueError(
            "reranker model is required; pass --reranker-model or set RAG_RERANKER_MODEL"
        )

    dedicated_base = str(values.get("RAG_RERANKER_BASE_URL") or "").strip().rstrip("/")
    dedicated_key = str(values.get("RAG_RERANKER_API_KEY") or "").strip()
    if bool(dedicated_base) != bool(dedicated_key):
        raise ValueError(
            "RAG_RERANKER_BASE_URL and RAG_RERANKER_API_KEY must be set together"
        )
    if dedicated_base and dedicated_key:
        return LiveRerankerConfig(
            model=selected_model,
            base_url=dedicated_base,
            api_key=dedicated_key,
            profile="rerank-env",
        )

    base_url = str(values.get("RAG_EMBEDDING_BASE_URL") or "").strip().rstrip("/")
    api_key = str(values.get("RAG_EMBEDDING_API_KEY") or "").strip()
    if not base_url or not api_key:
        raise ValueError(
            "reranker transport requires RAG_RERANKER_* or RAG_EMBEDDING_* base/key"
        )
    return LiveRerankerConfig(
        model=selected_model,
        base_url=base_url,
        api_key=api_key,
        profile="embedding-env",
    )


def build_live_reranker(
    config: LiveRerankerConfig,
    *,
    timeout_seconds: float = 30.0,
    client_factory: Any = httpx.Client,
) -> tuple[SiliconFlowReranker, Any]:
    """Build a closeable HTTP client and SiliconFlow reranker adapter."""

    if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    client = client_factory(
        base_url=config.base_url,
        headers={"Authorization": f"Bearer {config.api_key}"},
        timeout=timeout_seconds,
    )
    return SiliconFlowReranker(client, model=config.model), client
