import pytest
from leon_agent.models import resolve_model_id


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("GPT-5.6-LUNA", "GPT-5.6-LUNA"),
        ("DeepSeek-V4-Flash-0731", "DeepSeek-V4-Flash-0731"),
        ("future-model-2027", "future-model-2027"),
        ("", None),
    ],
)
def test_resolve_model_id(value: str, expected: str | None) -> None:
    assert resolve_model_id(value) == expected


def test_numeric_model_shortcut_uses_dynamic_catalog() -> None:
    models = ["Provider-Model-A", "Provider-Model-B"]

    assert resolve_model_id("1", models) == "Provider-Model-A"
    assert resolve_model_id("2", models) == "Provider-Model-B"
    assert resolve_model_id("3", models) is None
