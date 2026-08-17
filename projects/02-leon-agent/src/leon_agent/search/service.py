"""Channel-independent web-search service."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from workbench_core.agent.runtime import AgentCancelled, current_cancel_event

from leon_agent.search.provider import SearchProvider


def _check_cancelled() -> None:
    event = current_cancel_event()
    if event is not None and event.is_set():
        raise AgentCancelled("agent turn cancelled")


def _source_name(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


class WebSearchService:
    """Validate search requests and normalize provider data for the LLM."""

    def __init__(self, provider: SearchProvider, *, default_max_results: int = 5) -> None:
        if not 1 <= default_max_results <= 10:
            raise ValueError("default_max_results must be between 1 and 10")
        self.provider = provider
        self.default_max_results = default_max_results

    def search(
        self,
        query: str,
        max_results: int | None = None,
        search_depth: str = "basic",
        topic: str = "general",
    ) -> dict[str, Any]:
        _check_cancelled()
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return {"ok": False, "error": "搜索关键词不能为空"}
        if len(normalized_query) > 500:
            return {"ok": False, "error": "搜索关键词不能超过 500 个字符"}

        try:
            result_limit = self.default_max_results if max_results is None else int(max_results)
        except (TypeError, ValueError):
            return {"ok": False, "error": "max_results 必须是整数"}
        if not 1 <= result_limit <= 10:
            return {"ok": False, "error": "max_results 必须在 1 到 10 之间"}
        if search_depth not in {"basic", "advanced"}:
            return {"ok": False, "error": "search_depth 只能是 basic 或 advanced"}
        if topic not in {"general", "news"}:
            return {"ok": False, "error": "topic 只能是 general 或 news"}

        try:
            raw = self.provider.search(
                normalized_query,
                max_results=result_limit,
                search_depth=search_depth,
                topic=topic,
            )
        except AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - provider is an external boundary
            return {
                "ok": False,
                "provider": getattr(self.provider, "name", "unknown"),
                "error": str(exc),
            }
        _check_cancelled()

        raw_items = raw.get("results", []) if isinstance(raw, Mapping) else []
        provider_name = getattr(self.provider, "name", "unknown")
        reported_provider = raw.get("_leon_provider") if isinstance(raw, Mapping) else None
        if isinstance(reported_provider, str) and reported_provider.strip():
            provider_name = reported_provider.strip()
        results: list[dict[str, Any]] = []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, Mapping):
                continue
            url = _text(item.get("url"), limit=2048)
            if not url.startswith(("http://", "https://")):
                continue
            results.append(
                {
                    "title": _text(item.get("title"), limit=300),
                    "url": url,
                    "snippet": _text(item.get("content"), limit=700),
                    "source": _source_name(url),
                    "published_at": _text(item.get("published_date"), limit=64) or None,
                }
            )

        return {
            "ok": True,
            "provider": provider_name,
            "query": normalized_query,
            "search_depth": search_depth,
            "topic": topic,
            "results": results[:result_limit],
            "searched_at": datetime.now(timezone.utc).isoformat(),
        }
