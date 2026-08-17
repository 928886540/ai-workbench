import { reactive, readonly, ref } from "vue";
import { api } from "../api/client";
import { stripImageLinks } from "./markdown";

export type SpeechStatus = "idle" | "loading" | "playing";

interface SpeechOperation {
  messageId: string;
  token: symbol;
  controller: AbortController | null;
  objectUrl: string | null;
  /** A gateway clip URL. Unlike a Blob URL, this must never be revoked. */
  remoteUrl: string | null;
  onComplete?: () => void;
}

interface PendingSpeech {
  messageId: string;
  objectUrl: string | null;
  remoteUrl?: string | null;
  onComplete?: () => void;
}

type QueuedSpeechState = "loading" | "ready" | "failed";

interface QueuedSpeechChunk {
  messageId: string;
  token: symbol;
  controller: AbortController | null;
  state: QueuedSpeechState;
  blob: Blob | null;
  cancelled: boolean;
}

const SILENT_MP3 =
  "data:audio/mpeg;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA//tAwAAAAAAAAAAAAAAAAAAAAAAASW5mbwAAAA8AAAAEAAABIADAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMD/////////////////////////////////////////////AAAAAExhdmM1OC4xMwAAAAAAAAAAAAAAACQCQAAAAAAAAAEg0Jj//8QAAAAAAAAAAAAAAAAAAAAA//sQxAADwAABpAAAACAAADSAAAAETEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV";

const statusState = reactive<Record<string, SpeechStatus>>({});
const errorState = reactive<Record<string, string>>({});
const TTS_CACHE_LIMIT = 10;
const ttsBlobCache = new Map<string, Blob>();

export const speechStatus = readonly(statusState);
export const speechError = readonly(errorState);
export const audioUnlockRequired = ref(false);

let player: HTMLAudioElement | null = null;
let audioUnlocked = false;
let unlockPromise: Promise<boolean> | null = null;
let active: SpeechOperation | null = null;
let pending: PendingSpeech | null = null;
let queuedMessageId: string | null = null;
const queuedChunks: QueuedSpeechChunk[] = [];

export function setVoiceMediaSessionMetadata(title: string, artist = "Leon Agent"): void {
  if (
    typeof navigator === "undefined" ||
    !("mediaSession" in navigator) ||
    typeof MediaMetadata === "undefined"
  ) {
    return;
  }
  navigator.mediaSession.metadata = new MediaMetadata({
    title: title.trim() || "Leon 语音",
    artist: artist.trim() || "Leon Agent",
    album: "Leon Agent",
    artwork: [
      {
        src: new URL("/icon-512.png", window.location.origin).href,
        sizes: "512x512",
        type: "image/png",
      },
    ],
  });
}

function ttsCacheKey(text: string, voiceId?: string): string {
  return `${voiceId || "default"}\u0000${text}`;
}

function getCachedTts(key: string): Blob | null {
  const cached = ttsBlobCache.get(key) || null;
  if (cached) {
    ttsBlobCache.delete(key);
    ttsBlobCache.set(key, cached);
  }
  return cached;
}

function cacheTts(key: string, blob: Blob): void {
  ttsBlobCache.delete(key);
  ttsBlobCache.set(key, blob);
  while (ttsBlobCache.size > TTS_CACHE_LIMIT) {
    const oldest = ttsBlobCache.keys().next().value;
    if (typeof oldest !== "string") break;
    ttsBlobCache.delete(oldest);
  }
}

function getPlayer(): HTMLAudioElement | null {
  if (player) return player;
  if (typeof Audio === "undefined") return null;
  player = new Audio();
  player.preload = "auto";
  player.setAttribute("playsinline", "");
  return player;
}

function setStatus(messageId: string, status: SpeechStatus): void {
  statusState[messageId] = status;
}

function revokeObjectUrl(url: string | null): void {
  if (url && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(url);
}

function sourceUrl(operation: SpeechOperation): string | null {
  return operation.remoteUrl || operation.objectUrl;
}

function cleanupOperation(operation: SpeechOperation): void {
  // Remote `/api/voice/clips/` URLs belong to the gateway and stay usable by
  // the native audio control after singleton playback stops. Only local TTS
  // Blob URLs are ours to revoke.
  revokeObjectUrl(operation.objectUrl);
}

function cleanupPending(waiting: PendingSpeech): void {
  revokeObjectUrl(waiting.objectUrl);
}

function finishActive(operation: SpeechOperation): void {
  if (active?.token !== operation.token) return;
  active = null;
  cleanupOperation(operation);
  setStatus(operation.messageId, "idle");
  operation.onComplete?.();
}

async function playOperation(operation: SpeechOperation): Promise<boolean> {
  const audio = getPlayer();
  const url = sourceUrl(operation);
  if (!audio || !url) {
    errorState[operation.messageId] = "当前浏览器不支持音频播放";
    finishActive(operation);
    return false;
  }

  const complete = (): void => finishActive(operation);
  audio.onended = complete;
  audio.onerror = complete;
  audio.src = url;
  audio.currentTime = 0;
  try {
    await audio.play();
    if (active?.token !== operation.token) return false;
    audioUnlocked = true;
    audioUnlockRequired.value = false;
    setStatus(operation.messageId, "playing");
    return true;
  } catch (error) {
    if (active?.token !== operation.token) return false;
    if (error instanceof DOMException && error.name === "NotAllowedError") {
      active = null;
      if (pending) cleanupPending(pending);
      if (operation.remoteUrl) {
        pending = {
          messageId: operation.messageId,
          objectUrl: operation.objectUrl,
          remoteUrl: operation.remoteUrl,
          onComplete: operation.onComplete,
        };
      } else {
        // Keep the Blob-only shape explicit: local TTS URLs are the only
        // pending payload that can ever need revocation.
        pending = {
          messageId: operation.messageId,
          objectUrl: operation.objectUrl,
          onComplete: operation.onComplete,
        };
      }
      audioUnlockRequired.value = true;
      setStatus(operation.messageId, "idle");
      return false;
    }
    errorState[operation.messageId] = error instanceof Error ? error.message : "音频播放失败";
    finishActive(operation);
    return false;
  }
}

async function playPendingSpeech(): Promise<boolean> {
  const waiting = pending;
  if (!waiting) return true;
  pending = null;
  const operation: SpeechOperation = {
    messageId: waiting.messageId,
    token: Symbol(waiting.messageId),
    controller: null,
    objectUrl: waiting.objectUrl,
    remoteUrl: waiting.remoteUrl || null,
    onComplete: waiting.onComplete,
  };
  active = operation;
  setStatus(operation.messageId, "loading");
  return playOperation(operation);
}

export function getSpeechStatus(messageId: string): SpeechStatus {
  return statusState[messageId] || "idle";
}

export function getSpeechError(messageId: string): string {
  return errorState[messageId] || "";
}

/**
 * Prime the one reusable audio element inside a user gesture. On iOS the
 * unlocked state belongs to the element itself, so creating a new Audio per
 * response would be blocked again.
 */
export async function unlockAudio(): Promise<boolean> {
  if (audioUnlocked) return playPendingSpeech();
  const audio = getPlayer();
  if (!audio) return false;
  if (!unlockPromise) {
    unlockPromise = (async () => {
      try {
        audio.src = SILENT_MP3;
        await audio.play();
        audio.pause();
        audio.currentTime = 0;
        audioUnlocked = true;
        audioUnlockRequired.value = false;
      } catch {
        audioUnlocked = false;
      }
      return audioUnlocked;
    })();
  }
  try {
    const unlocked = await unlockPromise;
    return unlocked ? playPendingSpeech() : false;
  } finally {
    unlockPromise = null;
  }
}

function cancelQueuedSpeech(messageId?: string): void {
  if (!queuedMessageId || (messageId && queuedMessageId !== messageId)) return;
  const cancelledMessageId = queuedMessageId;
  queuedMessageId = null;
  for (const chunk of queuedChunks.splice(0)) {
    chunk.cancelled = true;
    chunk.controller?.abort();
    chunk.controller = null;
    chunk.blob = null;
  }
  setStatus(cancelledMessageId, "idle");
}

function pumpQueuedSpeech(): void {
  if (!queuedMessageId || active || pending) return;

  let chunk = queuedChunks[0];
  while (chunk && (chunk.cancelled || chunk.state === "failed")) {
    queuedChunks.shift();
    chunk = queuedChunks[0];
  }
  if (!chunk) {
    const messageId = queuedMessageId;
    queuedMessageId = null;
    setStatus(messageId, "idle");
    return;
  }
  if (chunk.state === "loading") {
    setStatus(chunk.messageId, "loading");
    return;
  }

  queuedChunks.shift();
  const blob = chunk.blob;
  chunk.blob = null;
  if (!blob) {
    chunk.state = "failed";
    pumpQueuedSpeech();
    return;
  }
  const operation: SpeechOperation = {
    messageId: chunk.messageId,
    token: chunk.token,
    controller: null,
    objectUrl: URL.createObjectURL(blob),
    remoteUrl: null,
    onComplete: pumpQueuedSpeech,
  };
  active = operation;
  setStatus(operation.messageId, "loading");
  void playOperation(operation);
}

async function prepareQueuedSpeechChunk(
  chunk: QueuedSpeechChunk,
  text: string,
  voiceId?: string,
): Promise<boolean> {
  const cacheKey = ttsCacheKey(text, voiceId);
  const cachedBlob = getCachedTts(cacheKey);
  try {
    const blob =
      cachedBlob || (await api.synthesizeSpeech(text, voiceId, chunk.controller?.signal));
    if (chunk.cancelled) return false;
    chunk.controller = null;
    if (!cachedBlob) cacheTts(cacheKey, blob);
    chunk.blob = blob;
    chunk.state = "ready";
    pumpQueuedSpeech();
    return true;
  } catch (error) {
    chunk.controller = null;
    if (chunk.cancelled || (error instanceof DOMException && error.name === "AbortError")) {
      return false;
    }
    chunk.state = "failed";
    errorState[chunk.messageId] = error instanceof Error ? error.message : "朗读失败";
    // A failed head must never prevent later, already synthesized chunks from playing.
    pumpQueuedSpeech();
    return false;
  }
}

/**
 * Synthesize streamed text chunks concurrently and play them in enqueue order.
 * A new message takes ownership of the shared player; repeated chunks for the
 * same message join its queue without interrupting the chunk already playing.
 */
export function queueSpeechChunk(
  messageId: string,
  rawText: string,
  voiceId?: string,
): Promise<boolean> {
  const text = speakableText(rawText);
  if (!text) return Promise.resolve(false);

  if (queuedMessageId && queuedMessageId !== messageId) {
    stopSpeech();
  } else if (!queuedMessageId && (active || pending)) {
    stopSpeech();
  }
  queuedMessageId = messageId;
  delete errorState[messageId];
  setVoiceMediaSessionMetadata(text);

  const cacheKey = ttsCacheKey(text, voiceId);
  const chunk: QueuedSpeechChunk = {
    messageId,
    token: Symbol(messageId),
    controller: getCachedTts(cacheKey) ? null : new AbortController(),
    state: "loading",
    blob: null,
    cancelled: false,
  };
  queuedChunks.push(chunk);
  setStatus(messageId, active?.messageId === messageId ? "playing" : "loading");
  return prepareQueuedSpeechChunk(chunk, text, voiceId);
}

export function stopSpeech(messageId?: string): void {
  cancelQueuedSpeech(messageId);
  if (pending && (!messageId || pending.messageId === messageId)) {
    const waiting = pending;
    pending = null;
    cleanupPending(waiting);
    setStatus(waiting.messageId, "idle");
  }

  if (active && (!messageId || active.messageId === messageId)) {
    const operation = active;
    active = null;
    operation.controller?.abort();
    const audio = getPlayer();
    audio?.pause();
    cleanupOperation(operation);
    setStatus(operation.messageId, "idle");
  }
  if (!pending) audioUnlockRequired.value = false;
}

export async function speakMessage(
  messageId: string,
  rawText: string,
  voiceId?: string,
): Promise<boolean> {
  const text = speakableText(rawText);
  if (!text) {
    errorState[messageId] = "没有可朗读的文字";
    return false;
  }

  setVoiceMediaSessionMetadata(text);

  stopSpeech();
  delete errorState[messageId];
  const cacheKey = ttsCacheKey(text, voiceId);
  const cachedBlob = getCachedTts(cacheKey);
  const controller = cachedBlob ? null : new AbortController();
  const operation: SpeechOperation = {
    messageId,
    token: Symbol(messageId),
    controller,
    objectUrl: null,
    remoteUrl: null,
  };
  active = operation;
  setStatus(messageId, "loading");
  try {
    const blob = cachedBlob || await api.synthesizeSpeech(text, voiceId, controller?.signal);
    if (active?.token !== operation.token) return false;
    operation.controller = null;
    if (!cachedBlob) cacheTts(cacheKey, blob);
    operation.objectUrl = URL.createObjectURL(blob);
    return playOperation(operation);
  } catch (error) {
    if (active?.token !== operation.token) return false;
    active = null;
    setStatus(messageId, "idle");
    if (!(error instanceof DOMException && error.name === "AbortError")) {
      errorState[messageId] = error instanceof Error ? error.message : "朗读失败";
    }
    return false;
  }
}

/**
 * Normalize a gateway voice clip URL before placing it in an audio element.
 * Only same-origin `/api/voice/clips/{clip_id}` paths are accepted; any
 * caller-supplied token is replaced with the current session token.
 */
export function buildVoiceClipUrl(rawUrl: string, token?: string): string | null {
  const raw = String(rawUrl || "").trim();
  if (!raw) return null;
  const origin = typeof window === "undefined" ? "http://localhost" : window.location.origin;
  let parsed: URL;
  try {
    parsed = new URL(raw, origin);
  } catch {
    return null;
  }
  if (
    parsed.origin !== origin ||
    !/^\/api\/voice\/clips\/[^/?#]+$/.test(parsed.pathname)
  ) {
    return null;
  }
  if (token !== undefined) {
    parsed.searchParams.delete("token");
    const cleanToken = String(token || "").trim();
    if (cleanToken) parsed.searchParams.set("token", cleanToken);
  }
  parsed.hash = "";
  return `${parsed.pathname}${parsed.search}`;
}

/** Play an already-generated gateway clip through the shared singleton. */
export async function playVoiceClip(
  messageId: string,
  url: string,
  token?: string,
): Promise<boolean> {
  const safeUrl = buildVoiceClipUrl(url, token);
  if (!safeUrl) {
    errorState[messageId] = "语音地址不受信任";
    setStatus(messageId, "idle");
    return false;
  }
  stopSpeech();
  delete errorState[messageId];
  const operation: SpeechOperation = {
    messageId,
    token: Symbol(messageId),
    controller: null,
    objectUrl: null,
    remoteUrl: safeUrl,
  };
  active = operation;
  setStatus(messageId, "loading");
  return playOperation(operation);
}

// Name used by a few callers that describe the payload as a remote clip.
export const playRemoteClip = playVoiceClip;

export function clearSpeechState(): void {
  stopSpeech();
  for (const key of Object.keys(statusState)) delete statusState[key];
  for (const key of Object.keys(errorState)) delete errorState[key];
}

/** Strip content that should not be read aloud, preserving human prose. */
export function speakableText(raw: string): string {
  return stripImageLinks(String(raw || ""))
    .replace(/\r\n?/g, "\n")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/https?:\/\/\S+/gi, " ")
    .replace(/\s*(?:提交信息|详细信息|任务信息)\s*[:：]\s*(?=\n|$)/g, "")
    .replace(
      /(?:(?:任务|生成计划|客户端任务)\s*(?:ID|编号)|(?:job|generation\s+plan|client\s+task)\s*id)\s*[:：]\s*[A-Za-z0-9_-]+/gi,
      " ",
    )
    .replace(/[（(]\s*(?=[A-Za-z0-9_-]*[_\d])[A-Za-z0-9][A-Za-z0-9_-]{5,}\s*[）)]/g, " ")
    .replace(/\b[a-fA-F0-9]{16,}\b/g, " ")
    .replace(/\b[A-Za-z][A-Za-z0-9_-]{19,}\b/g, " ")
    .replace(/[\u{1F000}-\u{1FAFF}\u2600-\u27BF\uFE0E\uFE0F\u200D]/gu, " ")
    .replace(/^\s*(?:[-*+•]\s+|[-–—]{2,}\s*$)/gm, "")
    .replace(/[`*_>#|~]/g, "")
    .split("\n")
    .map((line) =>
      line
        .replace(/[ \t]+/g, " ")
        .replace(/([\u3400-\u9fff」』）]) (?=[\u3400-\u9fff「『（])/g, "$1")
        .replace(/^[ ，]+|[ ，]+$/g, ""),
    )
    .filter(Boolean)
    .join("，")
    .replace(/，{2,}/g, "，")
    .replace(/^[ ，]+|[ ，]+$/g, "");
}

if (typeof document !== "undefined") {
  document.addEventListener("pointerdown", () => void unlockAudio(), {
    once: true,
    capture: true,
  });
}
