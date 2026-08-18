"""Public contracts for the RAG lab."""

from rag_lab.benchmark import (
    BenchmarkCaseResult,
    BenchmarkResult,
    RetrievalCase,
    RetrievalDataset,
    load_retrieval_dataset,
    run_retrieval_benchmark,
)
from rag_lab.chunking import Chunk, TextDocument, chunk_document
from rag_lab.citations import CitationContext, build_citation_context
from rag_lab.embeddings import (
    EmbeddedChunk,
    EmbeddingError,
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
    embed_chunks,
    embed_query,
)
from rag_lab.evaluation import RetrievalScore, citation_precision, score_retrieval
from rag_lab.faithfulness import (
    ClaimVerdict,
    FaithfulnessError,
    FaithfulnessJudge,
    FaithfulnessReport,
    OpenAIFaithfulnessJudge,
    evaluate_faithfulness,
)
from rag_lab.generation import (
    AnswerGenerationError,
    AnswerGenerator,
    DeterministicFakeAnswerGenerator,
    OpenAIAnswerGenerator,
    extract_citations,
)
from rag_lab.pipeline import RAGAnswer, answer_query
from rag_lab.reranking import Reranker, RerankingError, SiliconFlowReranker
from rag_lab.retrieval import RetrievalHit, VectorRetriever
from rag_lab.suite import RAGSuiteCaseResult, RAGSuiteResult, run_rag_suite
from rag_lab.tools import RAGSearchService, Retriever, create_rag_search_tool

__all__ = [
    "BenchmarkCaseResult",
    "BenchmarkResult",
    "AnswerGenerationError",
    "AnswerGenerator",
    "Chunk",
    "ClaimVerdict",
    "CitationContext",
    "DeterministicFakeAnswerGenerator",
    "EmbeddedChunk",
    "EmbeddingError",
    "EmbeddingProvider",
    "FaithfulnessError",
    "FaithfulnessJudge",
    "FaithfulnessReport",
    "OpenAIEmbeddingProvider",
    "OpenAIAnswerGenerator",
    "OpenAIFaithfulnessJudge",
    "RAGAnswer",
    "RAGSuiteCaseResult",
    "RAGSuiteResult",
    "RAGSearchService",
    "Reranker",
    "RerankingError",
    "RetrievalHit",
    "RetrievalCase",
    "RetrievalDataset",
    "RetrievalScore",
    "Retriever",
    "TextDocument",
    "VectorRetriever",
    "build_citation_context",
    "answer_query",
    "chunk_document",
    "citation_precision",
    "create_rag_search_tool",
    "embed_chunks",
    "embed_query",
    "evaluate_faithfulness",
    "extract_citations",
    "load_retrieval_dataset",
    "run_retrieval_benchmark",
    "run_rag_suite",
    "SiliconFlowReranker",
    "score_retrieval",
]
