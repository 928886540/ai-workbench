"""Adapter for the Volink OpenAI-compatible TTS API.

The provider ships no public documentation, so the contract below was derived by
probing the live service (2026-08-15):

    GET  /v1/tts/models                  -> {"models": [{id, name, ...}]}
    GET  /v1/tts/voices?page_size=&page= -> {"voices": [...], "total_count", "has_more"}
    POST /v1/audio/speech                -> raw audio/mpeg bytes

Two behaviours are easy to get wrong and are pinned by tests:
* `voice` must be the 24-hex voice id. Human names and provider-internal names
  (for example "woman_fengyun") answer 404.
* Every voice is bound to one TTS model via its own `model` field. Sending a
  different model still returns 200 but renders a different voice, so callers
  should always take the model from the voice entry.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

VOICE_ID_LENGTH = 24

_TTS_LABELED_ID_RE = re.compile(
    r"(?i)(?:(?:任务|生成计划|客户端任务)\s*(?:ID|编号)|"
    r"(?:job|generation\s+plan|client\s+task)\s*id)\s*[:：]\s*[A-Za-z0-9_-]+"
)
_TTS_CODE_PARENS_RE = re.compile(
    r"[（(]\s*(?=[A-Za-z0-9_-]*[_\d])[A-Za-z0-9][A-Za-z0-9_-]{5,}\s*[）)]"
)
_TTS_LONG_HEX_RE = re.compile(r"\b[a-fA-F0-9]{16,}\b")
_TTS_LONG_IDENTIFIER_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]{19,}\b")
_TTS_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0E\uFE0F\u200D]"
)


def prepare_speech_text(value: str) -> str:
    """Remove markup and internal identifiers that TTS would pronounce literally."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*(?:提交信息|详细信息|任务信息)\s*[:：]\s*(?=\n|$)", "", text)
    text = _TTS_LABELED_ID_RE.sub(" ", text)
    text = _TTS_CODE_PARENS_RE.sub(" ", text)
    text = _TTS_LONG_HEX_RE.sub(" ", text)
    text = _TTS_LONG_IDENTIFIER_RE.sub(" ", text)
    text = _TTS_EMOJI_RE.sub(" ", text)
    text = re.sub(r"(?m)^\s*(?:[-*+•]\s+|[-–—]{2,}\s*$)", "", text)
    text = re.sub(r"[`*_>#|~]", "", text)
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip(" ，")
        line = re.sub(
            r"([\u3400-\u9fff」』）]) (?=[\u3400-\u9fff「『（])",
            r"\1",
            line,
        )
        if line:
            lines.append(line)
    return re.sub(r"，{2,}", "，", "，".join(lines)).strip(" ，")


class VoiceError(RuntimeError):
    pass


@dataclass
class _CatalogCache:
    ttl_seconds: float
    fetched_at: float = 0.0
    payload: dict[str, Any] | None = field(default=None)

    def get(self) -> dict[str, Any] | None:
        if self.payload is None or time.monotonic() - self.fetched_at > self.ttl_seconds:
            return None
        return self.payload

    def set(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.fetched_at = time.monotonic()

    def clear(self) -> None:
        self.payload = None
        self.fetched_at = 0.0


class VolinkVoiceClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.volink.org/v1",
        timeout_seconds: float = 60.0,
        catalog_ttl_seconds: float = 900.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise VoiceError("Volink API key is not configured")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._http = http_client or httpx.Client(timeout=timeout_seconds)
        self._catalog = _CatalogCache(ttl_seconds=catalog_ttl_seconds)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self._http.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                params=params,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise VoiceError(f"Volink HTTP {exc.response.status_code}: {path}") from exc
        except httpx.HTTPError as exc:
            raise VoiceError(f"Volink unavailable: {exc}") from exc
        except ValueError as exc:
            raise VoiceError(f"Volink returned invalid JSON: {path}") from exc

    def list_models(self) -> list[dict[str, Any]]:
        payload = self._get("/tts/models")
        models = payload.get("models") if isinstance(payload, dict) else None
        return [item for item in (models or []) if isinstance(item, dict)]

    def list_voices(self, *, page_size: int = 100, max_pages: int = 10) -> list[dict[str, Any]]:
        """Walk the paginated voice catalogue.

        `page_size` is the only pagination knob the service honours; `limit`,
        `per_page` and `size` are silently ignored and fall back to 20.

        `lang=zh-CN` is required for Chinese display names — without it the
        catalogue answers with English ones ("Elegant Lady" instead of 风韵少妇),
        which makes the names unsearchable for a Chinese-speaking user. Note that
        `locale=zh-CN` and `lang=zh` do NOT work; only `lang=zh-CN`.
        """
        voices: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            payload = self._get(
                "/tts/voices",
                {"page_size": page_size, "page": page, "lang": "zh-CN"},
            )
            if not isinstance(payload, dict):
                break
            batch = [item for item in (payload.get("voices") or []) if isinstance(item, dict)]
            fresh = [item for item in batch if str(item.get("id") or "") not in seen]
            if not fresh:
                break
            for item in fresh:
                seen.add(str(item.get("id") or ""))
            voices.extend(fresh)
            if not payload.get("has_more"):
                break
        return voices

    def catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        """Models plus voices, trimmed to the fields the client actually renders."""
        if refresh:
            self._catalog.clear()
        cached = self._catalog.get()
        if cached is not None:
            return cached
        models = self.list_models()
        voices = self.list_voices()
        payload = {
            "models": [
                {"id": item.get("id"), "name": item.get("name")}
                for item in models
                if item.get("id")
            ],
            "voices": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "model": item.get("model"),
                    "languages": item.get("languages") or [],
                    "demo": item.get("demo"),
                }
                for item in voices
                if item.get("id")
            ],
        }
        self._catalog.set(payload)
        return payload

    def resolve_voice(self, voice_id: str) -> dict[str, Any] | None:
        target = str(voice_id or "").strip()
        if not target:
            return None
        for item in self.catalog().get("voices", []):
            if item.get("id") == target:
                return item
        return None

    def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        model: str | None = None,
        speed: float | None = None,
    ) -> bytes:
        """Render text to mp3 bytes.

        When `model` is omitted the voice's own model is used, because a
        mismatched pair still returns 200 while rendering the wrong voice.
        """
        clean_text = prepare_speech_text(text)
        if not clean_text:
            raise VoiceError("text contains no speakable content")
        clean_voice = str(voice_id or "").strip()
        if not clean_voice:
            raise VoiceError("voice_id is required for speech synthesis")

        chosen_model = model
        if not chosen_model:
            voice = self.resolve_voice(clean_voice)
            chosen_model = (voice or {}).get("model")
        if not chosen_model:
            raise VoiceError(f"Cannot resolve a TTS model for voice {clean_voice}")

        payload: dict[str, Any] = {
            "model": chosen_model,
            "input": clean_text,
            "voice": clean_voice,
            "response_format": "mp3",
        }
        if speed is not None:
            payload["speed"] = speed
        try:
            response = self._http.post(
                f"{self.base_url}/audio/speech",
                headers=self._headers(),
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:200].strip() or f"HTTP {exc.response.status_code}"
            raise VoiceError(f"Volink speech failed: {detail}") from exc
        except httpx.HTTPError as exc:
            raise VoiceError(f"Volink unavailable: {exc}") from exc

        audio = response.content
        if not audio:
            raise VoiceError("Volink returned an empty audio payload")
        return audio
