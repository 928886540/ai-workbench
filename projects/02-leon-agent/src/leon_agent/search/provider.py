"""Provider adapters for Leon's web-search tool."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from workbench_core.agent.runtime import AgentCancelled, current_cancel_event


class SearchProvider(Protocol):
    """Small synchronous provider contract used by the Agent tool boundary."""

    def search(
        self,
        query: str,
        *,
        max_results: int,
        search_depth: str,
        topic: str,
    ) -> Mapping[str, Any]: ...


def _check_cancelled() -> None:
    event = current_cancel_event()
    if event is not None and event.is_set():
        raise AgentCancelled("agent turn cancelled")


class TavilySearchProvider:
    """HTTP adapter for Tavily's search endpoint.

    The API key is sent only in the Authorization header and never included in
    the returned tool result or exception text.
    """

    name = "tavily"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        timeout_seconds: float = 15.0,
    ) -> None:
        self.api_key = api_key.strip()
        if not self.api_key:
            raise ValueError("Tavily API key is required")
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url:
            raise ValueError("Tavily base URL is required")
        if timeout_seconds <= 0:
            raise ValueError("Tavily timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    def search(
        self,
        query: str,
        *,
        max_results: int,
        search_depth: str,
        topic: str,
    ) -> Mapping[str, Any]:
        _check_cancelled()
        payload = {
            "query": query,
            "search_depth": search_depth,
            "topic": topic,
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        try:
            response = httpx.post(
                f"{self.base_url}/search",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise RuntimeError(f"Tavily search failed with HTTP {status}") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Tavily search request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise RuntimeError("Tavily returned invalid JSON") from exc
        _check_cancelled()
        if not isinstance(data, dict):
            raise RuntimeError("Tavily returned a non-object response")
        return data
