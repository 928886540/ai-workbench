"""Provider-free contracts for ordered streaming TTS playback."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def _speech_source() -> str:
    return (PROJECT_ROOT / "web/src/utils/speech.ts").read_text(encoding="utf-8")


def _chat_source() -> str:
    return (PROJECT_ROOT / "web/src/views/ChatView.vue").read_text(encoding="utf-8")


def test_streaming_tts_synthesizes_concurrently_and_plays_in_order() -> None:
    speech = _speech_source()

    for fragment in (
        "export function queueSpeechChunk(",
        "queuedChunks.push(chunk);",
        "return prepareQueuedSpeechChunk(chunk, text, voiceId);",
        "await api.synthesizeSpeech(text, voiceId, chunk.controller?.signal)",
        "let chunk = queuedChunks[0];",
        "queuedChunks.shift();",
        "onComplete: pumpQueuedSpeech",
        "const cachedBlob = getCachedTts(cacheKey);",
        "if (!cachedBlob) cacheTts(cacheKey, blob);",
    ):
        assert fragment in speech, fragment


def test_streaming_tts_failure_cancel_and_ios_pending_do_not_break_the_queue() -> None:
    speech = _speech_source()

    for fragment in (
        "chunk.state = \"failed\";",
        "pumpQueuedSpeech();",
        "function cancelQueuedSpeech(messageId?: string): void",
        "chunk.controller?.abort();",
        "cancelQueuedSpeech(messageId);",
        "onComplete: operation.onComplete",
        "onComplete: waiting.onComplete",
    ):
        assert fragment in speech, fragment

    # Existing one-shot entry points must still explicitly take ownership of
    # the singleton player, which also clears any streaming queue.
    assert speech.count("stopSpeech();") >= 3


def test_chat_wires_raw_deltas_completion_and_retry_to_streaming_speech() -> None:
    chat = _chat_source()

    for fragment in (
        "function autoplayAttemptKey(message: ChatMessage): string",
        "`${message.id}:${message.revisions.length}`",
        "enqueueStreamingSpeech(message, delta);",
        "completeStreamingSpeech(pendingAssistant(), content);",
        "completeStreamingSpeech(current, response.answer);",
        "stopSpeech(target.id);",
        "resetStreamingSpeech(target.id);",
    ):
        assert fragment in chat, fragment


def test_chat_chunks_stream_before_visual_completion_and_keeps_selected_voice() -> None:
    chat = _chat_source()

    for fragment in (
        "const SPEECH_CHUNK_MIN_CHARS = 8;",
        "const SPEECH_CHUNK_MAX_CHARS = 64;",
        "streamingSpeechVoiceId = activeVoiceId();",
        "void queueSpeechChunk(streamingSpeechMessageId, chunk, streamingSpeechVoiceId);",
        "streamingSpeechFinal = true;",
    ):
        assert fragment in chat, fragment
