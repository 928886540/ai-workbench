from __future__ import annotations

from types import SimpleNamespace

import pytest
from rag_lab.providers import (
    DeterministicFakeEmbeddingProvider,
    LiveEmbeddingConfig,
    LiveRerankerConfig,
    build_live_embedding_provider,
    build_live_reranker,
    resolve_live_embedding_config,
    resolve_live_reranker_config,
)


def test_fake_provider_is_deterministic() -> None:
    provider = DeterministicFakeEmbeddingProvider(dimensions=32)

    first = provider.embed(["RAG 检索"])[0]
    second = provider.embed(["RAG 检索"])[0]

    assert first == second
    assert len(first) == 32
    assert any(first)


def test_live_config_requires_dedicated_embedding_model() -> None:
    with pytest.raises(ValueError, match="embedding model"):
        resolve_live_embedding_config(environ={})


def test_live_config_uses_dedicated_env_pair() -> None:
    config = resolve_live_embedding_config(
        environ={
            "RAG_EMBEDDING_MODEL": "BAAI/bge-m3",
            "RAG_EMBEDDING_BASE_URL": "https://embedding.example/v1/",
            "RAG_EMBEDDING_API_KEY": "sk-private",
        }
    )

    assert config.model == "BAAI/bge-m3"
    assert config.base_url == "https://embedding.example/v1"
    assert config.api_key == "sk-private"
    assert config.profile == "rag-env"
    assert "sk-private" not in repr(config)


def test_live_config_rejects_partial_dedicated_env() -> None:
    with pytest.raises(ValueError, match="must be set together"):
        resolve_live_embedding_config(
            model="BAAI/bge-m3",
            environ={"RAG_EMBEDDING_BASE_URL": "https://embedding.example/v1"},
        )


def test_live_config_can_reuse_shared_lab_credentials() -> None:
    settings = SimpleNamespace(
        active_base_url="https://shared.example/v1",
        profile="toml:test",
        require_api_key=lambda: "sk-shared",
    )

    config = resolve_live_embedding_config(
        model="text-embedding-3-small",
        environ={},
        shared_settings=settings,
    )

    assert config.base_url == "https://shared.example/v1"
    assert config.api_key == "sk-shared"
    assert config.profile == "shared:toml:test"


def test_build_live_provider_passes_config_without_leaking_key() -> None:
    calls = []

    def client_factory(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        return SimpleNamespace(embeddings=SimpleNamespace())

    config = LiveEmbeddingConfig(
        model="text-embedding-3-small",
        base_url="https://embedding.example/v1",
        api_key="sk-private",
    )
    provider, client = build_live_embedding_provider(
        config,
        client_factory=client_factory,
    )

    assert provider.model == "text-embedding-3-small"
    assert client.embeddings is not None
    assert calls == [
        {
            "api_key": "sk-private",
            "base_url": "https://embedding.example/v1",
            "timeout": 30.0,
            "max_retries": 0,
        }
    ]


def test_live_reranker_config_prefers_dedicated_transport() -> None:
    config = resolve_live_reranker_config(
        environ={
            "RAG_RERANKER_MODEL": "BAAI/bge-reranker-v2-m3",
            "RAG_RERANKER_BASE_URL": "https://rerank.example/v1/",
            "RAG_RERANKER_API_KEY": "sk-rerank",
            "RAG_EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "RAG_EMBEDDING_API_KEY": "sk-embedding",
        }
    )

    assert config.model == "BAAI/bge-reranker-v2-m3"
    assert config.base_url == "https://rerank.example/v1"
    assert config.api_key == "sk-rerank"
    assert config.profile == "rerank-env"
    assert "sk-rerank" not in repr(config)


def test_live_reranker_config_reuses_embedding_transport() -> None:
    config = resolve_live_reranker_config(
        model="reranker",
        environ={
            "RAG_EMBEDDING_BASE_URL": "https://embedding.example/v1/",
            "RAG_EMBEDDING_API_KEY": "sk-embedding",
        },
    )

    assert config.base_url == "https://embedding.example/v1"
    assert config.api_key == "sk-embedding"
    assert config.profile == "embedding-env"


def test_live_reranker_config_rejects_missing_model_or_transport() -> None:
    with pytest.raises(ValueError, match="reranker model"):
        resolve_live_reranker_config(environ={})
    with pytest.raises(ValueError, match="requires.*base/key"):
        resolve_live_reranker_config(model="reranker", environ={})


@pytest.mark.parametrize(
    "environ",
    [
        {"RAG_RERANKER_BASE_URL": "https://rerank.example/v1"},
        {"RAG_RERANKER_API_KEY": "sk-rerank"},
        {"RAG_EMBEDDING_BASE_URL": "https://embedding.example/v1"},
        {"RAG_EMBEDDING_API_KEY": "sk-embedding"},
    ],
)
def test_live_reranker_config_rejects_partial_pairs(
    environ: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        resolve_live_reranker_config(model="reranker", environ=environ)


def test_build_live_reranker_sets_bearer_header() -> None:
    calls = []

    def client_factory(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        return SimpleNamespace()

    config = LiveRerankerConfig(
        model="reranker",
        base_url="https://rerank.example/v1",
        api_key="sk-private",
    )
    reranker, client = build_live_reranker(
        config,
        timeout_seconds=12.5,
        client_factory=client_factory,
    )

    assert reranker.model == "reranker"
    assert client is not None
    assert calls == [
        {
            "base_url": "https://rerank.example/v1",
            "headers": {"Authorization": "Bearer sk-private"},
            "timeout": 12.5,
        }
    ]


@pytest.mark.parametrize("timeout", [0, -1, True])
def test_build_live_reranker_rejects_invalid_timeout(timeout: float) -> None:
    config = LiveRerankerConfig(
        model="reranker",
        base_url="https://rerank.example/v1",
        api_key="sk-private",
    )

    with pytest.raises(ValueError, match="timeout"):
        build_live_reranker(config, timeout_seconds=timeout)
