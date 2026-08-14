import pytest
from leon_agent.models import MODEL_IDS, resolve_model_id


def test_model_catalog_contains_requested_models() -> None:
    assert "gpt-5.6-luna" in MODEL_IDS
    assert "gpt-5.6-sol" in MODEL_IDS
    assert "glm-5.2" in MODEL_IDS
    assert "gemini-3.1-pro-preview" in MODEL_IDS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", "gemini-3.1-pro-preview"),
        ("10", "gpt-5.6-sol"),
        ("GPT-5.6-LUNA", "gpt-5.6-luna"),
        ("future-model-2027", "future-model-2027"),
        ("", None),
        ("99", None),
    ],
)
def test_resolve_model_id(value: str, expected: str | None) -> None:
    assert resolve_model_id(value) == expected
