"""Provider-free contracts for the Vue Agent Timeline panel."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_timeline_records_protocol_events_without_stream_delta_noise() -> None:
    chat = _read("web/src/views/ChatView.vue")

    for fragment in (
        "const timelineOpen = ref(false);",
        "const timelineEntries = ref<TimelineEntry[]>([]);",
        "const MAX_TIMELINE_ENTRIES = 100;",
        "function recordTimelineEvent(event: LeonEvent): void",
        'if (event.event === "assistant.delta") return;',
        "timelineEntries.value.push({",
        "if (timelineEntries.value.length > MAX_TIMELINE_ENTRIES)",
        "timelineEntries.value.splice(0, timelineEntries.value.length - MAX_TIMELINE_ENTRIES);",
        "recordTimelineEvent(event);",
        'eventName.startsWith("session.")',
        'eventName.startsWith("user.")',
        'eventName.startsWith("assistant.")',
        'eventName.startsWith("tool.")',
        'eventName.startsWith("image.")',
        'eventName.startsWith("voice.")',
        'eventName === "agent.error"',
    ):
        assert fragment in chat, fragment


def test_timeline_toggle_and_details_are_accessible_plain_text_vue_bindings() -> None:
    chat = _read("web/src/views/ChatView.vue")

    for fragment in (
        'class="header-icon-button timeline-toggle"',
        'aria-controls="timeline-panel"',
        ':aria-expanded="timelineOpen"',
        "@click=\"toggleTimeline\"",
        'id="timeline-panel"',
        'role="dialog"',
        'aria-label="运行记录"',
        "@keydown.escape=\"closeTimeline\"",
        'v-for="entry in timelineEntries"',
        ':key="entry.id"',
        ':data-kind="entry.kind"',
        "{{ entry.label }}",
        "{{ entry.detail }}",
        "{{ entry.time }}",
        "@click=\"clearTimeline()\"",
        "暂无事件",
    ):
        assert fragment in chat, fragment

    panel = chat.split('<aside', 1)[1].split('</aside>', 1)[0]
    assert "v-html" not in panel


def test_timeline_resets_for_new_session_logout_and_unmount() -> None:
    chat = _read("web/src/views/ChatView.vue")

    assert "function clearTimeline(close = false): void" in chat
    assert "clearTimeline(true);" in chat
    assert chat.count("clearTimeline(true);") >= 3
    assert "timelineEntries.value = [];" in chat
    assert "timelineSequence = 0;" in chat


def test_timeline_panel_is_compact_and_mobile_friendly() -> None:
    styles = _read("web/src/styles.css")

    assert ".timeline-panel,\n.session-history-panel {" in styles
    for selector in (".timeline-list", ".timeline-entry__detail"):
        assert f"{selector} {{" in styles, selector
    panel_start = styles.index(".timeline-panel,")
    panel = styles[panel_start : styles.index("}", panel_start) + 1]
    assert "position: absolute;" in panel
    assert "overflow-y: auto;" in panel
    assert "z-index: 25;" in panel
