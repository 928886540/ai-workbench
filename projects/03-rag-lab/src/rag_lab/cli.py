"""Command-line retrieval evaluation with fake-by-default provider behavior."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from rag_lab.benchmark import (
    DEFAULT_DATASET_PATH,
    load_retrieval_dataset,
    run_retrieval_benchmark,
)
from rag_lab.providers import (
    DeterministicFakeEmbeddingProvider,
    build_live_embedding_provider,
    build_live_reranker,
    resolve_live_embedding_config,
    resolve_live_reranker_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the fixed RAG retrieval baseline")
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly allow calls to a real OpenAI-compatible embedding API",
    )
    parser.add_argument(
        "--model",
        help="dedicated embedding model; required in live mode (or RAG_EMBEDDING_MODEL)",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="rerank a larger live candidate pool before scoring",
    )
    parser.add_argument("--reranker-model", help="dedicated reranker model")
    parser.add_argument("--candidate-k", type=int, default=8)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.rerank and not args.live:
        print("reranking requires explicit --live opt-in", file=sys.stderr)
        return 2
    client = None
    reranker_client = None
    reranker = None
    if args.live:
        try:
            config = resolve_live_embedding_config(model=args.model)
            provider, client = build_live_embedding_provider(
                config,
                timeout_seconds=args.timeout,
            )
            if args.rerank:
                reranker_config = resolve_live_reranker_config(model=args.reranker_model)
                reranker, reranker_client = build_live_reranker(
                    reranker_config,
                    timeout_seconds=args.timeout,
                )
        except (TypeError, ValueError, OSError) as exc:
            if client is not None:
                client.close()
            if reranker_client is not None:
                reranker_client.close()
            print(f"live embedding configuration failed: {exc}", file=sys.stderr)
            return 2
        provider_name = f"live:{config.profile}:{config.model}"
        reranker_name = (
            f"live:{reranker_config.profile}:{reranker_config.model}"
            if reranker is not None
            else None
        )
    else:
        provider = DeterministicFakeEmbeddingProvider()
        provider_name = "fake:deterministic-lexical-v1"
        reranker_name = None

    try:
        dataset = load_retrieval_dataset(args.dataset)
        result = run_retrieval_benchmark(
            dataset,
            provider,
            top_k=args.top_k,
            batch_size=args.batch_size,
            reranker=reranker,
            candidate_k=args.candidate_k,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"retrieval benchmark failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
        if reranker_client is not None:
            reranker_client.close()

    output = {"provider": provider_name, "reranker": reranker_name, **result.as_dict()}
    if args.as_json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"provider={provider_name}")
        print(f"reranker={reranker_name or 'none'}")
        print(
            f"cases={result.case_count} top_k={result.top_k} "
            f"recall@k={result.recall_at_k:.3f} "
            f"mrr={result.mean_reciprocal_rank:.3f}"
        )
    return 0
