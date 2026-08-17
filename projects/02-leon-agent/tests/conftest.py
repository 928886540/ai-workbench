from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def isolated_user_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., None]:
    """Give Gateway tests a deterministic user config instead of real profiles.

    Runtime config is authoritative, so tests that need a non-default value must
    write it into this temporary TOML rather than setting an ambient environment
    variable that ``apply_config_file`` will deliberately clear.
    """
    path = tmp_path / "leon-config.toml"

    defaults: dict[str, str] = {
        "LLM_TIMEOUT_SECONDS": "0",
        "LLM_MAX_RETRIES": "0",
        "LEON_SESSION_DB": str(tmp_path / "test.db"),
        "LEON_API_TOKEN": "",
        "LEON_ASR_BASE_URL": "",
        "LEON_ASR_TOKEN": "",
        "LEON_FILE_ROOTS": "{}",
    }

    def configure(**overrides: Any) -> None:
        values = {**defaults, **{key: str(value) for key, value in overrides.items()}}
        payload = (
            'model_provider = "test"\n'
            'model = "test-model"\n\n'
            '[model_providers.test]\n'
            'base_url = "http://127.0.0.1:9/v1"\n'
            'experimental_bearer_token = "test-provider-key"\n\n'
            '[leon.env]\n'
        )
        payload += "".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
            for key, value in values.items()
        )
        path.write_text(payload, encoding="utf-8")

    configure()
    monkeypatch.setenv("LEON_CONFIG_FILE", str(path))
    return configure
