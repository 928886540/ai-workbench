import json
import sqlite3
from pathlib import Path

from workbench_core.ccs import list_providers, resolve_provider


def _create_ccs_db(path: Path) -> None:
    config = json.dumps(
        {
            "auth": {"OPENAI_API_KEY": "sk-test-provider-key"},
            "config": 'model = "gpt-test"\nbase_url = "https://api.example.com/v1"',
        }
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE providers (
                id TEXT PRIMARY KEY,
                app_type TEXT NOT NULL,
                name TEXT NOT NULL,
                is_current INTEGER NOT NULL,
                settings_config TEXT NOT NULL,
                notes TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO providers
                (id, app_type, name, is_current, settings_config, notes)
            VALUES (?, 'codex', ?, ?, ?, NULL)
            """,
            [
                ("bohe-level3", "薄荷 level3", 1, config),
                ("bohe-codex", "薄荷codex", 0, config),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_list_codex_providers_from_local_ccs(tmp_path: Path) -> None:
    db_path = tmp_path / "cc-switch.db"
    _create_ccs_db(db_path)

    providers = list_providers(app_type="codex", db_path=db_path)
    assert len(providers) == 2
    assert all(p.base_url and p.api_key for p in providers)


def test_resolve_bohe_fuzzy_prefers_current(tmp_path: Path) -> None:
    db_path = tmp_path / "cc-switch.db"
    _create_ccs_db(db_path)

    provider = resolve_provider("薄荷", app_type="codex", db_path=db_path)

    assert provider.name == "薄荷 level3"
    assert provider.is_current is True
    assert provider.base_url == "https://api.example.com/v1"


def test_resolve_bohe_fuzzy_prefers_token_prefix(tmp_path: Path) -> None:
    db_path = tmp_path / "cc-switch.db"
    _create_ccs_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE providers SET is_current = 0")

    provider = resolve_provider("薄荷", app_type="codex", db_path=db_path)

    assert provider.name == "薄荷 level3"
