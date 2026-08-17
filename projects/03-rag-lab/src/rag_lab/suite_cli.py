"""Run a batch RAG suite with fake-by-default provider behavior."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from openai import OpenAI
from workbench_core.config import Settings

from rag_lab.benchmark import DEFAULT_DATASET_PATH, load_retrieval_dataset
from rag_lab.faithfulness import OpenAIFaithfulnessJudge
from rag_lab.generation import (
    DeterministicFakeAnswerGenerator,
    OpenAIAnswerGenerator,
)
from rag_lab.providers import (
    DeterministicFakeEmbeddingProvider,
    build_live_embedding_provider,
    build_live_reranker,
    resolve_live_embedding_config,
    resolve_live_reranker_config,
)
from rag_lab.suite import run_rag_suite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the batch RAG evaluation suite")
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
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="run only this case id; repeat to select multiple cases",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--context-max-chars", type=int, default=4_000)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="rerank a larger live candidate pool before suite scoring",
    )
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
            embedding_provider_name = (
                f"live:{embedding_config.profile}:{embedding_config.model}"
            )
            answer_provider_name = f"live:{settings.profile}:{answer_model}"
            reranker_provider_name = (
                f"live:{reranker_config.profile}:{reranker_config.model}"
                if reranker is not None
                else None
            )
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
        embedding_provider_name = "fake:deterministic-lexical-v1"
        answer_provider_name = "fake:deterministic-answer-v1"
        reranker_provider_name = None

    try:
        result = run_rag_suite(
            load_retrieval_dataset(args.dataset),
            embedding_provider,
            answer_generator,
            faithfulness_judge=faithfulness_judge,
            case_ids=args.case_ids,
            top_k=args.top_k,
            batch_size=args.batch_size,
            context_max_chars=args.context_max_chars,
            reranker=reranker,
            candidate_k=args.candidate_k,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"RAG suite failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if embedding_client is not None:
            embedding_client.close()
        if answer_client is not None:
            answer_client.close()
        if reranker_client is not None:
            reranker_client.close()

    output = {
        "embedding_provider": embedding_provider_name,
        "answer_provider": answer_provider_name,
        "judge_provider": judge_provider_name,
        "reranker_provider": reranker_provider_name,
        **result.as_dict(),
    }
    if args.as_json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"embedding_provider={embedding_provider_name}")
        print(f"answer_provider={answer_provider_name}")
        print(f"reranker_provider={reranker_provider_name or 'none'}")
        print(
            f"cases={result.case_count} top_k={result.top_k} "
            f"recall@k={result.recall_at_k:.3f} "
            f"mrr={result.mean_reciprocal_rank:.3f}"
        )
        print(
            f"citation_rate={result.citation_rate:.3f} "
            f"citation_precision={result.mean_citation_precision:.3f}"
        )
        rendered_faithfulness = (
            "n/a"
            if result.mean_faithfulness is None
            else f"{result.mean_faithfulness:.3f}"
        )
        print(
            f"faithfulness={rendered_faithfulness} "
            f"judged_cases={len(result.faithfulness_scores)}"
        )
        unsupported = [
            (case.case_id, claim)
            for case in result.cases
            if case.answer.faithfulness is not None
            for claim in case.answer.faithfulness.claims
            if not claim.supported
        ]
        if result.faithfulness_scores:
            print(f"unsupported_claims={len(unsupported)}")
        for case_id, claim in unsupported:
            print(f"unsupported case={case_id}: {claim.claim}")
            print(f"reason={claim.reason}")
    return 0
