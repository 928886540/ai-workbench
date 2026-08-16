"""Optional web-search capability for Leon Agent."""

from __future__ import annotations

from leon_agent.search.provider import TavilySearchProvider
from leon_agent.search.service import WebSearchService


def create_search_service(
    *,
    api_key: str | None,
    base_url: str,
    timeout_seconds: float,
    max_results: int,
) -> WebSearchService | None:
    """Build the Tavily service only when a usable key is configured."""
    key = (api_key or "").strip()
    if not key:
        return None
    provider = TavilySearchProvider(
        api_key=key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    return WebSearchService(provider, default_max_results=max_results)


__all__ = [
    "TavilySearchProvider",
    "WebSearchService",
    "create_search_service",
]
