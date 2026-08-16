"""Static, provider-free contracts for the Vue text-to-speech migration."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_vue_tts_api_never_exposes_upstream_credentials() -> None:
    client = _read("web/src/api/client.ts")

    for fragment in (
        "async synthesizeSpeech(",
        '"/api/agent/tts"',
        "signal,",
        "return response.blob();",
        "if (response.status === 401)",
    ):
        assert fragment in client, fragment
    assert "VOLINK_API_KEY" not in client


def test_speech_service_owns_one_player_and_cleans_blob_urls() -> None:
    speech = _read("web/src/utils/speech.ts")

    for fragment in (
        'export type SpeechStatus = "idle" | "loading" | "playing";',
        "let player: HTMLAudioElement | null = null;",
        "player = new Audio();",
        "export const audioUnlockRequired = ref(false);",
        "pending = {",
        "objectUrl: operation.objectUrl",
        "operation.controller?.abort();",
        "URL.revokeObjectURL(url)",
        "export function clearSpeechState(): void",
    ):
        assert fragment in speech, fragment


def test_speech_text_cleanup_covers_urls_markdown_ids_and_emoji() -> None:
    speech = _read("web/src/utils/speech.ts")

    for fragment in (
        "export function speakableText(raw: string): string",
        'import { stripImageLinks } from "./markdown";',
        'return stripImageLinks(String(raw || ""))',
        ".replace(/",
        "https?:",
        "任务|生成计划|客户端任务",
        "1FAFF",
        '.join("，")',
    ):
        assert fragment in speech, fragment


def test_message_and_chat_wire_manual_and_automatic_speech() -> None:
    bubble = _read("web/src/components/MessageBubble.vue")
    chat = _read("web/src/views/ChatView.vue")

    for fragment in (
        "getSpeechStatus(props.message.id)",
        "speakMessage(props.message.id, displayedMessage.value.text, activeVoiceId())",
        "stopSpeech(props.message.id)",
        'class="message-speak"',
        'class="speech-spinner"',
        'class="speech-wave"',
        "void unlockAudio();",
    ):
        assert fragment in bubble, fragment
    for fragment in (
        "function maybeAutoplay(message: ChatMessage): void",
        "const autoplayRequests = new Set<string>();",
        'message.kind === "image-result"',
        "message.images.length > 0",
        "hasImageContent(message.text)",
        "void speakMessage(message.id, message.text, activeVoiceId());",
        "void ensureVoiceCatalog();",
        'v-if="audioUnlockRequired"',
        '@click="void unlockAudio()"',
    ):
        assert fragment in chat, fragment
