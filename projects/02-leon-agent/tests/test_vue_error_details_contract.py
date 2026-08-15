"""Provider-free contracts for collapsed Vue assistant error details."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_error_bubble_keeps_summary_visible_and_details_collapsed() -> None:
    bubble = _read("web/src/components/MessageBubble.vue")

    error_region = bubble.split('v-if="message.status === \'error\'"', 1)[1].split(
        'v-else-if="voiceClip"', 1
    )[0]
    for fragment in (
        'class="message-error" role="alert"',
        'class="message-error__summary">请求失败',
        '<details class="message-error__details">',
        "<summary>查看错误详情</summary>",
        '<pre class="message-error__raw">{{ message.text }}</pre>',
    ):
        assert fragment in error_region, fragment

    assert "<details open" not in error_region
    assert "v-html" not in error_region


def test_error_retry_toolbar_and_plain_text_styling_are_preserved() -> None:
    bubble = _read("web/src/components/MessageBubble.vue")
    styles = _read("web/src/styles.css")

    assert "message.role === 'agent' && message.status === 'error'" in bubble
    assert "@click=\"emit('retry', message.id)\"" in bubble
    for fragment in (
        ".message-error__details summary",
        ".message-error__raw",
        "white-space: pre-wrap;",
        "word-break: break-word;",
        "overflow: auto;",
    ):
        assert fragment in styles, fragment
