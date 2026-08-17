"""Optional web-search capability for Leon Agent."""

from __future__ import annotations

from leon_agent.search.provider import FailoverSearchProvider, TavilySearchProvider
from leon_agent.search.service import WebSearchService


def create_search_service(
    *,
    api_key: str | None,
    base_url: str,
    timeout_seconds: float,
    max_results: int,
    fallback_api_key: str | None = None,
    fallback_base_url: str | None = None,
) -> WebSearchService | None:
    """Build Tavily search with one optional HTTP fallback provider."""
    key = (api_key or "").strip()
    fallback_key = (fallback_api_key or "").strip()
    fallback_url = (fallback_base_url or "").strip()
    if not key and not (fallback_key and fallback_url):
        return None
    primary = (
        TavilySearchProvider(
            api_key=key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            name="tavily-primary",
        )
        if key
        else None
    )
    fallback = (
        TavilySearchProvider(
            api_key=fallback_key,
            base_url=fallback_url,
            timeout_seconds=timeout_seconds,
            name="tavily-fallback",
        )
        if fallback_key and fallback_url
        else None
    )
    provider = (
        FailoverSearchProvider(primary, fallback)
        if primary is not None and fallback is not None
        else primary or fallback
    )
    if provider is None:  # pragma: no cover - guarded by the key checks above
        return None
    return WebSearchService(provider, default_max_results=max_results)


__all__ = [
    "FailoverSearchProvider",
    "TavilySearchProvider",
    "WebSearchService",
    "create_search_service",
]
