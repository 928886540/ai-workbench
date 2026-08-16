from __future__ import annotations

import pytest
from leon_agent.gateway.app import ProviderPinLost, _resolve_pinned_snapshot


def test_legacy_ccs_provider_pin_is_rejected_without_resolution(monkeypatch) -> None:
    def unexpected_capture():
        raise AssertionError("legacy CCS pin must fail before reading any provider")

    monkeypatch.setattr(
        "leon_agent.gateway.app._capture_llm_snapshot",
        unexpected_capture,
    )

    with pytest.raises(ProviderPinLost, match="已与 CC Switch 脱钩"):
        _resolve_pinned_snapshot(
            ("ccs:legacy|https://legacy.example/v1", "https://legacy.example/v1")
        )
