"""Provider-free contract checks for the Vue composer auto-grow behavior."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_composer_auto_grow_clamps_to_five_line_budget() -> None:
    chat = _read("web/src/views/ChatView.vue")
    styles = _read("web/src/styles.css")

    for fragment in (
        "const COMPOSER_MIN_HEIGHT = 42;",
        "const COMPOSER_MAX_HEIGHT = 120;",
        "function resizeComposer(): void",
        "if (!textarea.value) {",
        "textarea.style.height = `${COMPOSER_MIN_HEIGHT}px`;",
        'textarea.style.height = "auto"',
        "Math.min(contentHeight, COMPOSER_MAX_HEIGHT)",
        'textarea.style.overflowY = contentHeight > COMPOSER_MAX_HEIGHT ? "auto" : "hidden"',
        "function handleComposerInput(): void",
        "function handleComposerFocus(): void",
        '@input="handleComposerInput"',
        '@focus="handleComposerFocus"',
    ):
        assert fragment in chat, fragment

    assert "max-height: 120px;" in styles
    assert "overflow-y: auto;" in styles
    assert "resize: none;" in styles


def test_composer_resets_after_send_and_cleans_inline_state_on_unmount() -> None:
    chat = _read("web/src/views/ChatView.vue")

    assert 'draft.value = "";' in chat
    assert "void nextTick(resizeComposer);" in chat
    assert "onBeforeUnmount(() => {" in chat
    assert 'composerInput.value.style.height = "";' in chat
    assert 'composerInput.value.style.overflowY = "";' in chat
    # Composer input uses Vue template bindings. The only window listener is
    # the explicitly cleaned-up online reconnect hook.
    assert "composerInput.addEventListener" not in chat
    assert 'window.addEventListener("online", handleOnline)' in chat
    assert 'window.removeEventListener("online", handleOnline)' in chat
