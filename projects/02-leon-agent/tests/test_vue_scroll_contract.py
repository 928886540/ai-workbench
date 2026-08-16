"""Provider-free contracts for Vue chat scroll-follow behavior.

These checks pin the source-level seam used by a fake Gateway.  They do not
start the Gateway, a browser, an image backend, or an LLM provider.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_chat_tracks_distance_from_bottom_without_global_scroll_listener() -> None:
    chat = _read("web/src/views/ChatView.vue")

    for fragment in (
        "const autoFollowMessages = ref(true);",
        "const showScrollToLatest = ref(false);",
        "const SCROLL_FOLLOW_THRESHOLD = 72;",
        "function updateMessageScrollState(): void",
        "const distanceFromBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight;",
        "const atLatest = distanceFromBottom <= SCROLL_FOLLOW_THRESHOLD;",
        "autoFollowMessages.value = atLatest;",
        "showScrollToLatest.value = !atLatest && panel.scrollHeight > panel.clientHeight;",
        "function handleMessagesScroll(): void",
        '@scroll="handleMessagesScroll"',
    ):
        assert fragment in chat, fragment

    assert "messagesPanel.addEventListener" not in chat
    assert 'window.addEventListener("scroll"' not in chat


def test_send_and_explicit_navigation_restore_follow_mode() -> None:
    chat = _read("web/src/views/ChatView.vue")

    for fragment in (
        "function scrollToLatest(force = false): void",
        "if (force) {",
        "autoFollowMessages.value = true;",
        "if (!force && !autoFollowMessages.value) return;",
        "if (view === \"chat\") scrollToLatest(true);",
        "scrollToLatest(true);",
        'aria-label="回到最新消息"',
        '@click="scrollToLatest(true)"',
    ):
        assert fragment in chat, fragment

    send_turn = chat.split("async function sendTurn", 1)[1].split(
        "async function sendMessage", 1
    )[0]
    assert send_turn.index("scrollToLatest(true);") > send_turn.index(
        'appendMessage(makeMessage("user", content));'
    )


def test_scroll_button_has_mobile_touch_target_and_unmount_keeps_cleanup_local() -> None:
    chat = _read("web/src/views/ChatView.vue")
    styles = _read("web/src/styles.css")

    assert 'class="messages-stage"' in chat
    assert 'class="scroll-to-latest"' in chat
    assert "v-if=\"showScrollToLatest\"" in chat
    assert "onBeforeUnmount(() => {" in chat
    assert "window.clearTimeout(modeBlurTimer);" in chat

    start = styles.index(".scroll-to-latest {")
    rule = styles[start : styles.index("}", start) + 1]
    assert "position: absolute;" in rule
    assert "min-height: 44px;" in rule
    assert "min-width: 44px;" in rule
