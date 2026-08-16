"""Provider-free contracts for agent-generated Vue voice clips.

These checks intentionally inspect source rather than booting a Gateway or
calling Volink.  The browser behavior is covered by the implementation's
small, explicit seams: messages[] is the source of truth, URL normalization
is centralized in speech.ts, and the visible bubble player owns playback.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_message_store_models_and_orders_voice_clips() -> None:
    messages = _read("web/src/stores/messages.ts")

    for fragment in (
        "export interface VoiceClip",
        "audio?: VoiceClip",
        "voice?: VoiceClip",
        "export const messages = ref<ChatMessage[]>([]);",
        "export function insertMessageBefore(",
        "export function hasVoiceClip(clipId: string): boolean",
        "message.audio?.clipId === clipId",
    ):
        assert fragment in messages, fragment


def test_chat_handles_voice_ready_and_delta_without_provider_calls() -> None:
    chat = _read("web/src/views/ChatView.vue")

    for fragment in (
        "function parseVoiceClip(data: Record<string, unknown>): VoiceClip | null",
        'const clipId = asString(data.clip_id).trim();',
        "buildVoiceClipUrl(rawUrl, api.token)",
        "if (!url) return null;",
        "function handleVoiceReady(data: Record<string, unknown>): void",
        "if (!clip || hasVoiceClip(clip.clipId)) return;",
        "insertMessageBefore(message, pending?.id || null);",
        'case "voice.ready":',
        'case "assistant.delta":',
        'const delta = asString(data.delta);',
        'message.status = "streaming";',
        "message.text += delta;",
        'finishAssistant(\n        asString(data.content) || pendingAssistant()?.text || "",\n'
        '        "done",\n        parseCompletedMeta(data),\n      );',
    ):
        assert fragment in chat, fragment

    assert "void playVoiceClip(message.id, clip.url, api.token);" not in chat

    # An explicit voice.ready event must not be gated by the text autoplay
    # preference; the rendered voice bubble decides whether autoplay succeeds.
    ready_region = chat.split('case "voice.ready":', 1)[0]
    assert "autoplayAll" not in ready_region[-700:]


def test_clip_urls_are_same_origin_and_remote_urls_are_not_revoked() -> None:
    speech = _read("web/src/utils/speech.ts")

    for fragment in (
        "export function buildVoiceClipUrl(rawUrl: string, token?: string): string | null",
        'new URL(raw, origin)',
        "parsed.origin !== origin",
        "^\\/api\\/voice\\/clips\\/[^/?#]+$",
        'parsed.searchParams.delete("token")',
        'parsed.searchParams.set("token", cleanToken)',
        "remoteUrl: string | null",
        "function cleanupOperation(operation: SpeechOperation): void",
        "revokeObjectUrl(operation.objectUrl);",
        "export async function playVoiceClip(",
        "export const playRemoteClip = playVoiceClip;",
    ):
        assert fragment in speech, fragment

    # The cleanup path is deliberately objectUrl-only; revoking a gateway URL
    # would invalidate the native <audio controls> element after autoplay.
    assert "revokeObjectUrl(operation.remoteUrl)" not in speech


def test_voice_bubble_owns_visible_custom_player_without_message_actions() -> None:
    bubble = _read("web/src/components/MessageBubble.vue")
    styles = _read("web/src/styles.css")

    for fragment in (
        "const voiceClip = computed(() => displayedMessage.value.audio || "
        "displayedMessage.value.voice || null);",
        "const isVoiceMessage = computed(() => voiceClip.value !== null);",
        'class="voice-bubble"',
        'class="voice-bubble__name"',
        'class="voice-bubble__size"',
        'class="voice-player"',
        'class="voice-player__toggle"',
        'aria-label="语音播放进度"',
        'class="voice-bubble__audio"',
        '@canplay="tryAutoplayVoice"',
        "await audio.play();",
        'class="voice-bubble__text"',
        "!isVoiceMessage &&",
    ):
        assert fragment in bubble, fragment
    assert " controls" not in bubble
    assert ".voice-bubble__audio" in styles and "display: none;" in styles
    assert ".voice-player__toggle" in styles

    # Editing now uses a separate dialog, but voice bubbles must still stay
    # outside the shared action toolbar so voice.ready cannot expose actions.
    assert 'v-if="!isVoiceMessage && showToolbar"' in bubble
