"""Provider-free contracts for Vue ``/nsfw --model`` completion.

The tests intentionally inspect source only.  They pin the fake-Gateway seam
and the mobile composer behavior without starting a Gateway or contacting an
LLM/image provider.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _css_rule(source: str, selector: str) -> str:
    start = source.index(f"{selector} {{")
    return source[start : source.index("}", start) + 1]


def test_vue_image_mode_catalog_uses_the_authenticated_api_helper() -> None:
    client = _read("web/src/api/client.ts")

    for fragment in (
        "export interface ImageMode {",
        "id: string;",
        "name: string;",
        "aliases?: string[];",
        "export interface ImageModesResponse {",
        "modes: ImageMode[];",
        "async getImageModes(signal?: AbortSignal): Promise<ImageModesResponse>",
        'this.request<ImageModesResponse>("/api/image-modes", { signal })',
    ):
        assert fragment in client, fragment


def test_mode_completion_matches_name_id_and_alias_without_duplicate_fetches() -> None:
    chat = _read("web/src/views/ChatView.vue")

    for fragment in (
        "function normalizeModeQuery(value: string): string",
        ".toLocaleLowerCase()",
        ".replace(/[\\s_.\\-·]+/g, \"\")",
        "function modeCompletionContext(value: string): ModeCompletionContext | null",
        "if (!/^\\/nsfw\\b/i.test(value)) return null;",
        "--model|-m",
        "(?:=|\\s+)([^\\s]*)$",
        "if (imageModeCatalog) return imageModeCatalog;",
        "if (!imageModeCatalogRequest)",
        ".getImageModes()",
        "requestId !== modeSuggestionRequestId || draft.value !== requestedValue",
        "const values = [item.name, item.id, ...(item.aliases || [])];",
        "normalizeModeQuery(value).includes(query)",
        "modeSuggestionsOpen.value = modeSuggestions.value.length > 0;",
    ):
        assert fragment in chat, fragment


def test_mode_completion_renders_an_accessible_keyboard_and_touch_picker() -> None:
    chat = _read("web/src/views/ChatView.vue")

    for fragment in (
        "function selectModeSuggestion(item: ImageMode): void",
        "draft.value = `${context.prefix} ${item.name} `;",
        "modeSuggestionIndex.value = (index + count) % count;",
        "if (modeSuggestionsOpen.value && modeSuggestions.value.length)",
        'event.key === "ArrowDown"',
        'event.key === "ArrowUp"',
        'event.key === "Escape"',
        "const selected = modeSuggestions.value[modeSuggestionIndex.value];",
        "if (selected) selectModeSuggestion(selected);",
        'ref="composerInput"',
        'class="mode-suggestions"',
        'role="listbox"',
        'aria-label="生图模式"',
        'v-for="(item, index) in modeSuggestions"',
        ':key="item.id"',
        'class="mode-suggestion"',
        'role="option"',
        ':aria-selected="index === modeSuggestionIndex"',
        ':class="{ active:',
        '@click="selectModeSuggestion(item)"',
        "{{ item.name }}",
        "{{ item.id }}",
        '@blur="scheduleHideModeSuggestions"',
    ):
        assert fragment in chat, fragment
    assert "@pointerdown.prevent" in chat or "@mousedown.prevent" in chat
    assert '@input="void updateModeSuggestions()"' in chat or '@input="handleComposerInput"' in chat
    if '@input="handleComposerInput"' in chat:
        handler = chat.split("function handleComposerInput", 1)[1].split(
            "function handleComposerFocus", 1
        )[0]
        assert "void updateModeSuggestions();" in handler

    keydown = chat.split("function handleComposerKeydown", 1)[1].split("onMounted", 1)[0]
    # Completion consumes Enter before the ordinary send branch.  Once the
    # picker is closed, the existing Enter-to-send behavior remains intact.
    assert keydown.index("modeSuggestionsOpen.value") < keydown.index(
        'if (event.key === "Enter" && !event.shiftKey)'
    )
    assert "activateModeSuggestion(modeSuggestionIndex.value + 1);" in keydown
    assert "activateModeSuggestion(modeSuggestionIndex.value - 1);" in keydown
    assert "hideModeSuggestions();" in keydown
    assert "void sendMessage();" in keydown


def test_mode_completion_closes_on_send_and_cleans_mobile_blur_state() -> None:
    chat = _read("web/src/views/ChatView.vue")
    styles = _read("web/src/styles.css")

    send = chat.split("async function sendMessage", 1)[1].split(
        "function handleComposerKeydown", 1
    )[0]
    assert send.index("hideModeSuggestions();") < send.index(
        "await sendTurn(content, { appendUser: true, retry: false });"
    )
    send_turn = chat.split("async function sendTurn", 1)[1].split(
        "async function sendMessage", 1
    )[0]
    assert 'appendMessage(makeMessage("user", content));' in send_turn

    unmount = chat.split("onBeforeUnmount(() => {", 1)[1].split("});", 1)[0]
    for fragment in (
        "if (modeBlurTimer !== null)",
        "window.clearTimeout(modeBlurTimer);",
        "modeBlurTimer = null;",
    ):
        assert fragment in unmount, fragment

    positioned_composer = (
        _css_rule(styles, ".composer-wrap")
        if ".composer-wrap {" in styles
        else _css_rule(styles, ".composer")
    )
    assert "position: relative;" in positioned_composer

    suggestions = _css_rule(styles, ".mode-suggestions")
    for fragment in ("position: absolute;", "overflow-y: auto;", "z-index:"):
        assert fragment in suggestions, fragment

    suggestion = _css_rule(styles, ".mode-suggestion")
    for fragment in ("display: flex;", "min-height: 48px;"):
        assert fragment in suggestion, fragment

    active = _css_rule(styles, ".mode-suggestion.active")
    assert "background:" in active
    assert any(
        marker in styles
        for marker in (
            ".mode-suggestion__name",
            ".mode-suggestion-name",
            ".mode-suggestion strong",
        )
    )
    assert any(
        marker in styles
        for marker in (
            ".mode-suggestion__id",
            ".mode-suggestion-id",
            ".mode-suggestion small",
        )
    )
