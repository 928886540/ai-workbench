"""Ask one question over the fixed corpus with fake-by-default providers."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from openai import OpenAI
from workbench_core.config import Settings

from rag_lab.benchmark import DEFAULT_DATASET_PATH, load_retrieval_dataset
from rag_lab.chunking import chunk_document
from rag_lab.embeddings import embed_chunks
from rag_lab.faithfulness import OpenAIFaithfulnessJudge
from rag_lab.generation import (
    DeterministicFakeAnswerGenerator,
    OpenAIAnswerGenerator,
)
from rag_lab.pipeline import answer_query
from rag_lab.providers import (
    DeterministicFakeEmbeddingProvider,
    build_live_embedding_provider,
    build_live_reranker,
    resolve_live_embedding_config,
    resolve_live_reranker_config,
)
from rag_lab.retrieval import VectorRetriever


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ask one grounded RAG question")
    parser.add_argument("query")
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly allow real embedding and chat provider calls",
    )
    parser.add_argument("--embedding-model", help="override RAG_EMBEDDING_MODEL")
    parser.add_argument("--answer-model", help="override the shared lab chat model")
    parser.add_argument(
        "--judge",
        action="store_true",
        help="run a real claim/evidence faithfulness judge; requires --live",
    )
    parser.add_argument("--judge-model", help="override the faithfulness judge model")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--context-max-chars", type=int, default=4_000)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--reranker-model", help="dedicated reranker model")
    parser.add_argument("--candidate-k", type=int, default=8)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.judge and not args.live:
        print("faithfulness judge requires explicit --live opt-in", file=sys.stderr)
        return 2
    if args.rerank and not args.live:
        print("reranking requires explicit --live opt-in", file=sys.stderr)
        return 2
    embedding_client = None
    answer_client = None
    reranker_client = None
    reranker = None
    faithfulness_judge = None
    judge_provider_name = None
    if args.live:
        try:
            embedding_config = resolve_live_embedding_config(
                model=args.embedding_model
            )
            embedding_provider, embedding_client = build_live_embedding_provider(
                embedding_config,
                timeout_seconds=args.timeout,
            )
            if args.rerank:
                reranker_config = resolve_live_reranker_config(model=args.reranker_model)
                reranker, reranker_client = build_live_reranker(
                    reranker_config,
                    timeout_seconds=args.timeout,
                )
            settings = Settings()
            answer_model = (args.answer_model or settings.active_model).strip()
            answer_client = OpenAI(
                api_key=settings.require_api_key(),
                base_url=settings.active_base_url,
                timeout=args.timeout,
                max_retries=0,
            )
            answer_generator = OpenAIAnswerGenerator(
                answer_client,
                model=answer_model,
            )
            if args.judge:
                judge_model = (args.judge_model or answer_model).strip()
                faithfulness_judge = OpenAIFaithfulnessJudge(
                    answer_client,
                    model=judge_model,
                )
                judge_provider_name = f"live:{settings.profile}:{judge_model}"
            provider_name = f"live:{embedding_config.profile}:{embedding_config.model}"
            reranker_name = (
                f"live:{reranker_config.profile}:{reranker_config.model}"
                if reranker is not None
                else None
            )
            answer_provider_name = f"live:{settings.profile}:{answer_model}"
        except (RuntimeError, TypeError, ValueError, OSError) as exc:
            if embedding_client is not None:
                embedding_client.close()
            if answer_client is not None:
                answer_client.close()
            if reranker_client is not None:
                reranker_client.close()
            print(f"live RAG configuration failed: {exc}", file=sys.stderr)
            return 2
    else:
        embedding_provider = DeterministicFakeEmbeddingProvider()
        answer_generator = DeterministicFakeAnswerGenerator()
        provider_name = "fake:deterministic-lexical-v1"
        reranker_name = None
        answer_provider_name = "fake:deterministic-answer-v1"

    try:
        dataset = load_retrieval_dataset(args.dataset)
        chunks = [
            chunk
            for document in dataset.documents
            for chunk in chunk_document(document)
        ]
        retriever = VectorRetriever(
            embed_chunks(chunks, embedding_provider, batch_size=args.batch_size),
            embedding_provider,
        )
        result = answer_query(
            args.query,
            retriever,
            answer_generator,
            top_k=args.top_k,
            context_max_chars=args.context_max_chars,
            faithfulness_judge=faithfulness_judge,
            reranker=reranker,
            candidate_k=args.candidate_k,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"RAG answer failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if embedding_client is not None:
            embedding_client.close()
        if answer_client is not None:
            answer_client.close()
        if reranker_client is not None:
            reranker_client.close()

    output = {
        "embedding_provider": provider_name,
        "answer_provider": answer_provider_name,
        "reranker": reranker_name,
        "judge_provider": judge_provider_name,
        **result.as_dict(),
    }
    if args.as_json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"embedding_provider={provider_name}")
        print(f"answer_provider={answer_provider_name}")
        print(f"reranker={reranker_name or 'none'}")
        print(result.text)
        print(
            f"citations={len(result.citations)} "
            f"citation_precision={result.citation_precision:.3f}"
        )
        if result.faithfulness is not None:
            score = result.faithfulness.score
            rendered_score = "n/a" if score is None else f"{score:.3f}"
            print(
                f"faithfulness={rendered_score} "
                f"supported_claims={result.faithfulness.supported_claims}/"
                f"{result.faithfulness.total_claims}"
            )
    return 0
