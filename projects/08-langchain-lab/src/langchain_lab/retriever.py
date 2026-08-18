"""Thin LangChain Retriever adapter over the existing RAG lab contract."""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field


class VectorRetrieverAdapter(BaseRetriever):
    """Convert RAG lab hits to LangChain Documents without owning retrieval."""

    backend: Any
    top_k: int = Field(default=3, ge=1)

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        del run_manager
        hits = self.backend.retrieve(query, top_k=self.top_k)
        return [
            Document(
                page_content=hit.chunk.text,
                metadata={
                    "citation": hit.citation,
                    "chunk_id": hit.chunk.chunk_id,
                    "root_id": hit.chunk.root_id,
                    "path": hit.chunk.path,
                    "start_line": hit.chunk.start_line,
                    "end_line": hit.chunk.end_line,
                    "score": float(hit.score),
                    "rank": hit.rank,
                    "untrusted_content": hit.chunk.untrusted_content,
                },
            )
            for hit in hits
        ]
