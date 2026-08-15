<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import AppStatus from "../components/AppStatus.vue";
import BottomNav, { type WorkbenchView } from "../components/BottomNav.vue";
import MessageBubble from "../components/MessageBubble.vue";
import { ApiError, api, type ImageMode, type LeonEvent } from "../api/client";
import GalleryView from "./GalleryView.vue";
import SettingsView from "./SettingsView.vue";
import TasksView from "./TasksView.vue";
import {
  appendMessage,
  clearMessages,
  findMessage,
  hasVoiceClip,
  insertMessageBefore,
  latestMessage,
  makeMessage,
  messages,
  type ChatMessage,
  type VoiceClip,
} from "../stores/messages";
import {
  clearImageState,
  completeImageTask,
  hydrateImageState,
  upsertImageTask,
} from "../stores/images";
import {
  activeVoiceId,
  autoplayAll,
  clearVoiceCatalog,
  loadVoiceCatalog,
} from "../stores/voice";
import {
  audioUnlockRequired,
  buildVoiceClipUrl,
  clearSpeechState,
  playVoiceClip,
  speakMessage,
  unlockAudio,
} from "../utils/speech";

const authenticated = ref(false);
const booting = ref(true);
const tokenInput = ref("");
const loginError = ref("");
const draft = ref("");
const sending = ref(false);
const connectionLabel = ref("未连接");
const connectionTone = ref<"neutral" | "ok" | "error">("neutral");
const taskStatus = ref("");
const activeView = ref<WorkbenchView>("chat");
const imageStateLoading = ref(false);
const imageStateLoaded = ref(false);
const imageStateError = ref("");
const previewUrl = ref("");
const previewDialog = ref<HTMLElement | null>(null);
const messagesPanel = ref<HTMLElement | null>(null);
const composerInput = ref<HTMLTextAreaElement | null>(null);
const modeSuggestionList = ref<HTMLElement | null>(null);
const timelinePanel = ref<HTMLElement | null>(null);
const timelineOpen = ref(false);
const timelineEntries = ref<TimelineEntry[]>([]);
const autoFollowMessages = ref(true);
const showScrollToLatest = ref(false);
const pendingAssistantId = ref<string | null>(null);
const modeSuggestions = ref<ImageMode[]>([]);
const modeSuggestionIndex = ref(0);
const modeSuggestionsOpen = ref(false);
let eventSource: EventSource | null = null;
let reconnectTimer: number | null = null;
let modeBlurTimer: number | null = null;
let imageModeCatalog: ImageMode[] | null = null;
let imageModeCatalogRequest: Promise<ImageMode[]> | null = null;
let modeSuggestionRequestId = 0;
let lastAgentErrorAt = 0;
const autoplayRequests = new Set<string>();
const COMPOSER_MIN_HEIGHT = 42;
const COMPOSER_MAX_HEIGHT = 120;
const SCROLL_FOLLOW_THRESHOLD = 72;
const MAX_TIMELINE_ENTRIES = 100;
let timelineSequence = 0;

interface ModeCompletionContext {
  prefix: string;
  partial: string;
}

type TimelineKind =
  | "session"
  | "user"
  | "assistant"
  | "tool"
  | "image"
  | "voice"
  | "error"
  | "event";

interface TimelineEntry {
  id: number;
  event: string;
  kind: TimelineKind;
  label: string;
  detail: string;
  time: string;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function timelineKind(eventName: string): TimelineKind {
  if (eventName.startsWith("session.")) return "session";
  if (eventName.startsWith("user.")) return "user";
  if (eventName.startsWith("assistant.")) return "assistant";
  if (eventName.startsWith("tool.")) return "tool";
  if (eventName.startsWith("image.")) return "image";
  if (eventName.startsWith("voice.")) return "voice";
  if (eventName === "agent.error") return "error";
  return "event";
}

function timelineLabel(eventName: string): string {
  const labels: Record<string, string> = {
    "session.connected": "会话连接",
    "user.message": "用户消息",
    "assistant.started": "助手开始",
    "assistant.completed": "助手回复",
    "assistant.notice": "助手提示",
    "tool.started": "工具开始",
    "tool.finished": "工具完成",
    "image.task.created": "图片任务创建",
    "image.task.updated": "图片任务更新",
    "image.completed": "图片完成",
    "voice.ready": "语音就绪",
    "agent.error": "请求错误",
  };
  return labels[eventName] || eventName;
}

function timelineValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function timelineDetail(event: LeonEvent): string {
  const data = event.data || {};
  const preferred = [
    "content",
    "error",
    "tool_name",
    "mode_name",
    "mode_id",
    "status",
    "voice_name",
    "job_id",
  ];
  const parts = preferred
    .filter((key) => data[key] !== undefined && data[key] !== null && data[key] !== "")
    .map((key) => `${key}: ${timelineValue(data[key])}`);
  if (!parts.length) {
    for (const [key, value] of Object.entries(data)) {
      parts.push(`${key}: ${timelineValue(value)}`);
    }
  }
  return parts.join("\n").slice(0, 1200);
}

function timelineTime(timestamp?: string): string {
  const date = timestamp ? new Date(timestamp) : new Date();
  return Number.isNaN(date.getTime())
    ? "--:--:--"
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function recordTimelineEvent(event: LeonEvent): void {
  // Deltas can arrive many times per answer; the started/completed entries are
  // enough to show the turn without turning the trace into a character log.
  if (event.event === "assistant.delta") return;
  timelineEntries.value.push({
    id: ++timelineSequence,
    event: event.event,
    kind: timelineKind(event.event),
    label: timelineLabel(event.event),
    detail: timelineDetail(event),
    time: timelineTime(event.timestamp),
  });
  if (timelineEntries.value.length > MAX_TIMELINE_ENTRIES) {
    timelineEntries.value.splice(0, timelineEntries.value.length - MAX_TIMELINE_ENTRIES);
  }
}

function clearTimeline(close = false): void {
  timelineEntries.value = [];
  timelineSequence = 0;
  if (close) timelineOpen.value = false;
}

function toggleTimeline(): void {
  timelineOpen.value = !timelineOpen.value;
  if (timelineOpen.value) {
    void nextTick(() => timelinePanel.value?.focus());
  }
}

function closeTimeline(): void {
  timelineOpen.value = false;
}

function normalizeModeQuery(value: string): string {
  return String(value || "")
    .toLocaleLowerCase()
    .replace(/[\s_.\-·]+/g, "");
}

function modeCompletionContext(value: string): ModeCompletionContext | null {
  if (!/^\/nsfw\b/i.test(value)) return null;
  const match = value.match(/^(.*(?:--model|-m))(?:=|\s+)?([^\s]*)$/i);
  return match ? { prefix: match[1], partial: match[2] || "" } : null;
}

async function ensureImageModeCatalog(): Promise<ImageMode[]> {
  if (imageModeCatalog) return imageModeCatalog;
  if (!imageModeCatalogRequest) {
    imageModeCatalogRequest = api
      .getImageModes()
      .then((payload) => {
        imageModeCatalog = Array.isArray(payload.modes) ? payload.modes : [];
        return imageModeCatalog;
      })
      .finally(() => {
        imageModeCatalogRequest = null;
      });
  }
  return imageModeCatalogRequest;
}

function hideModeSuggestions(): void {
  modeSuggestionRequestId += 1;
  modeSuggestionsOpen.value = false;
  modeSuggestions.value = [];
  modeSuggestionIndex.value = 0;
}

function activateModeSuggestion(index: number): void {
  if (!modeSuggestions.value.length) return;
  const count = modeSuggestions.value.length;
  modeSuggestionIndex.value = (index + count) % count;
  void nextTick(() => {
    const buttons = modeSuggestionList.value?.querySelectorAll<HTMLButtonElement>(
      ".mode-suggestion",
    );
    buttons?.[modeSuggestionIndex.value]?.scrollIntoView({ block: "nearest" });
  });
}

function selectModeSuggestion(item: ImageMode): void {
  const context = modeCompletionContext(draft.value);
  if (!context) return;
  draft.value = `${context.prefix} ${item.name} `;
  hideModeSuggestions();
  void nextTick(() => {
    resizeComposer();
    composerInput.value?.focus();
  });
}

function resizeComposer(): void {
  const textarea = composerInput.value;
  if (!textarea) return;
  if (!textarea.value) {
    textarea.style.height = `${COMPOSER_MIN_HEIGHT}px`;
    textarea.style.overflowY = "hidden";
    return;
  }
  textarea.style.height = "auto";
  const contentHeight = Math.max(textarea.scrollHeight, COMPOSER_MIN_HEIGHT);
  const nextHeight = Math.min(contentHeight, COMPOSER_MAX_HEIGHT);
  textarea.style.height = `${nextHeight}px`;
  textarea.style.overflowY = contentHeight > COMPOSER_MAX_HEIGHT ? "auto" : "hidden";
}

function handleComposerInput(): void {
  resizeComposer();
  void updateModeSuggestions();
}

function handleComposerFocus(): void {
  resizeComposer();
  void updateModeSuggestions();
}

async function updateModeSuggestions(): Promise<void> {
  if (modeBlurTimer !== null) {
    window.clearTimeout(modeBlurTimer);
    modeBlurTimer = null;
  }
  const requestedValue = draft.value;
  const context = modeCompletionContext(requestedValue);
  const requestId = ++modeSuggestionRequestId;
  if (!context) {
    hideModeSuggestions();
    return;
  }
  try {
    const modes = await ensureImageModeCatalog();
    if (requestId !== modeSuggestionRequestId || draft.value !== requestedValue) return;
    const query = normalizeModeQuery(context.partial);
    modeSuggestions.value = modes.filter((item) => {
      const values = [item.name, item.id, ...(item.aliases || [])];
      return !query || values.some((value) => normalizeModeQuery(value).includes(query));
    });
    modeSuggestionIndex.value = 0;
    modeSuggestionsOpen.value = modeSuggestions.value.length > 0;
  } catch {
    if (requestId === modeSuggestionRequestId) hideModeSuggestions();
  }
}

function scheduleHideModeSuggestions(): void {
  if (modeBlurTimer !== null) window.clearTimeout(modeBlurTimer);
  modeBlurTimer = window.setTimeout(() => {
    modeBlurTimer = null;
    hideModeSuggestions();
  }, 120);
}

function setConnection(label: string, tone: "neutral" | "ok" | "error"): void {
  connectionLabel.value = label;
  connectionTone.value = tone;
}

function updateMessageScrollState(): void {
  const panel = messagesPanel.value;
  if (!panel) return;
  const distanceFromBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight;
  const atLatest = distanceFromBottom <= SCROLL_FOLLOW_THRESHOLD;
  autoFollowMessages.value = atLatest;
  showScrollToLatest.value = !atLatest && panel.scrollHeight > panel.clientHeight;
}

function handleMessagesScroll(): void {
  updateMessageScrollState();
}

function scrollToLatest(force = false): void {
  if (force) {
    autoFollowMessages.value = true;
    showScrollToLatest.value = false;
  }
  if (!force && !autoFollowMessages.value) return;
  void nextTick(() => {
    const panel = messagesPanel.value;
    if (!panel) return;
    // Keep the existing smooth-scroll CSS for user navigation, but make event
    // driven follow-up immediate so a delta cannot expose an intermediate
    // position and accidentally disable follow mode.
    const previousBehavior = panel.style.scrollBehavior;
    panel.style.scrollBehavior = "auto";
    panel.scrollTop = panel.scrollHeight;
    panel.style.scrollBehavior = previousBehavior;
    updateMessageScrollState();
  });
}

function eventTime(timestamp?: string): number {
  const parsed = timestamp ? Date.parse(timestamp) : NaN;
  return Number.isFinite(parsed) ? parsed : Date.now();
}

async function loadImageState(force = false): Promise<void> {
  const sessionId = api.sessionId;
  if (
    !sessionId ||
    imageStateLoading.value ||
    (!force && imageStateLoaded.value)
  ) {
    return;
  }
  imageStateLoading.value = true;
  imageStateError.value = "";
  try {
    const state = await api.getImageState(sessionId);
    if (api.sessionId !== sessionId) return;
    hydrateImageState(state.tasks || [], state.images || []);
    imageStateError.value = Object.values(state.errors || {}).join("；");
    imageStateLoaded.value = true;
  } catch (error) {
    if (api.sessionId === sessionId) {
      imageStateError.value = error instanceof Error ? error.message : "无法加载图片状态";
    }
  } finally {
    if (api.sessionId === sessionId) imageStateLoading.value = false;
  }
}

function refreshImageState(): void {
  void loadImageState(true);
}

function selectView(view: WorkbenchView): void {
  hideModeSuggestions();
  activeView.value = view;
  if (view === "tasks" || view === "gallery") void loadImageState();
  if (view === "chat") scrollToLatest(true);
}

function retryMessage(messageId: string): void {
  if (sending.value) return;
  const index = messages.value.findIndex((message) => message.id === messageId);
  for (let cursor = (index >= 0 ? index : messages.value.length) - 1; cursor >= 0; cursor -= 1) {
    const candidate = messages.value[cursor];
    if (candidate.role !== "user" || !candidate.text.trim()) continue;
    draft.value = candidate.text.trim();
    void sendMessage();
    return;
  }
}

function editMessage(messageId: string, text: string): void {
  const message = findMessage(messageId);
  if (message) message.text = text;
}

function openPreview(url: string): void {
  previewUrl.value = url;
  void nextTick(() => previewDialog.value?.focus());
}

function closePreview(): void {
  previewUrl.value = "";
}

function closeEvents(): void {
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  eventSource?.close();
  eventSource = null;
}

function appendHistory(history: Array<{ role: string; content: string }>): void {
  clearMessages();
  for (const item of history) {
    if (item.role !== "user" && item.role !== "assistant") continue;
    appendMessage(makeMessage(item.role === "assistant" ? "agent" : "user", item.content));
  }
  scrollToLatest(true);
}

function pendingAssistant(): ChatMessage | null {
  return findMessage(pendingAssistantId.value);
}

function ensureVoiceCatalog(): Promise<boolean> {
  return loadVoiceCatalog().catch(() => false);
}

function maybeAutoplay(message: ChatMessage): void {
  if (
    message.role !== "agent" ||
    message.status !== "done" ||
    !message.text.trim() ||
    !autoplayAll.value ||
    autoplayRequests.has(message.id)
  ) {
    return;
  }
  autoplayRequests.add(message.id);
  void ensureVoiceCatalog().then((enabled) => {
    if (!enabled || !autoplayAll.value) {
      autoplayRequests.delete(message.id);
      return;
    }
    void speakMessage(message.id, message.text, activeVoiceId());
  });
}

function finishAssistant(content: string, status: "done" | "error" = "done"): void {
  let completed = pendingAssistant();
  if (completed) {
    completed.text = content;
    completed.status = status;
    completed.meta.finishedAt = Date.now();
    completed.meta.elapsedMs = completed.meta.startedAt
      ? completed.meta.finishedAt - completed.meta.startedAt
      : null;
  } else if (content) {
    completed = appendMessage(makeMessage("agent", content, status));
  }
  pendingAssistantId.value = null;
  taskStatus.value = "";
  if (completed) maybeAutoplay(completed);
  scrollToLatest();
}

function parseVoiceClip(data: Record<string, unknown>): VoiceClip | null {
  const clipId = asString(data.clip_id).trim();
  if (!clipId) return null;
  const rawUrl = asString(data.url).trim() || `/api/voice/clips/${encodeURIComponent(clipId)}`;
  const url = buildVoiceClipUrl(rawUrl, api.token);
  // Never put an untrusted URL into messages[]: MessageBubble also exposes it
  // to a native <audio> element, which cannot attach an Authorization header.
  if (!url) return null;
  const rawBytes = Number(data.bytes);
  return {
    clipId,
    url,
    text: asString(data.text),
    voiceId: asString(data.voice_id) || null,
    voiceName: asString(data.voice_name) || "语音",
    bytes: Number.isFinite(rawBytes) && rawBytes >= 0 ? Math.trunc(rawBytes) : 0,
  };
}

function handleVoiceReady(data: Record<string, unknown>): void {
  const clip = parseVoiceClip(data);
  if (!clip || hasVoiceClip(clip.clipId)) return;

  const message = makeMessage("agent", clip.text, "done");
  // A stable id makes keyed Vue updates deterministic if the same SSE payload
  // is replayed after a reconnect. The clip-id check above remains the source
  // of truth for de-duplication.
  message.id = `voice_${clip.clipId}`;
  message.audio = clip;
  message.voice = clip;
  const pending = pendingAssistant();
  insertMessageBefore(message, pending?.id || null);
  scrollToLatest();

  // This is an explicit agent request, not the user's "autoplay all answers"
  // preference. The singleton player handles browser unlock/pending playback.
  void playVoiceClip(message.id, clip.url, api.token);
}

function handleEvent(event: LeonEvent): void {
  const data = event.data || {};
  recordTimelineEvent(event);
  switch (event.event) {
    case "session.connected":
      setConnection("Gateway 已连接", "ok");
      break;
    case "user.message": {
      const content = asString(data.content);
      const last = latestMessage("user");
      if (content && last?.text !== content) appendMessage(makeMessage("user", content));
      break;
    }
    case "assistant.started": {
      const message = pendingAssistant() || appendMessage(makeMessage("agent", "", "pending"));
      message.status = "pending";
      message.meta.startedAt = Date.now();
      pendingAssistantId.value = message.id;
      taskStatus.value = "正在请求模型…";
      scrollToLatest();
      break;
    }
    case "assistant.delta": {
      const delta = asString(data.delta);
      if (!delta) break;
      const message = pendingAssistant() || appendMessage(makeMessage("agent", "", "streaming"));
      message.status = "streaming";
      message.meta.startedAt ||= Date.now();
      message.text += delta;
      pendingAssistantId.value = message.id;
      taskStatus.value = "正在生成…";
      scrollToLatest();
      break;
    }
    case "assistant.completed":
      finishAssistant(asString(data.content) || pendingAssistant()?.text || "");
      break;
    case "assistant.notice":
      if (data.content) appendMessage(makeMessage("agent", asString(data.content)));
      scrollToLatest();
      break;
    case "agent.error":
      lastAgentErrorAt = Date.now();
      finishAssistant(asString(data.error) || "请求失败", "error");
      break;
    case "tool.started":
      taskStatus.value = `正在执行：${asString(data.tool_name) || "工具"}`;
      break;
    case "tool.finished":
      taskStatus.value = asString(data.ok) === "false" ? "工具执行失败" : "工具已完成";
      break;
    case "image.task.created":
      upsertImageTask({ ...data, created_at: eventTime(event.timestamp) });
      taskStatus.value = "图片任务已提交，等待完成…";
      break;
    case "image.task.updated":
      upsertImageTask(data);
      taskStatus.value = `图片任务：${asString(data.status) || "处理中"}`;
      break;
    case "image.completed": {
      const imageUrl = asString(data.image_url);
      if (!imageUrl) break;
      const jobId = asString(data.job_id);
      if (jobId) completeImageTask(jobId, imageUrl, eventTime(event.timestamp));
      if (!messages.value.some((message) => message.images.includes(imageUrl))) {
        const target = appendMessage(makeMessage("agent"));
        target.images.push(imageUrl);
      }
      taskStatus.value = "";
      scrollToLatest();
      break;
    }
    case "voice.ready":
      handleVoiceReady(data);
      break;
    default:
      break;
  }
}

function connectEvents(): void {
  if (!api.sessionId) return;
  closeEvents();
  setConnection("正在连接…", "neutral");
  eventSource = api.connectEvents(
    api.sessionId,
    handleEvent,
    () => {
      eventSource?.close();
      eventSource = null;
      setConnection("连接断开", "error");
      if (authenticated.value && reconnectTimer === null) {
        reconnectTimer = window.setTimeout(() => {
          reconnectTimer = null;
          connectEvents();
        }, 3000);
      }
    },
  );
}

async function openSession(): Promise<void> {
  clearTimeline(true);
  let session: { messages: Array<{ role: string; content: string }> } | null = null;
  if (api.sessionId) {
    try {
      session = await api.getSession(api.sessionId);
    } catch {
      api.clearSession();
    }
  }
  if (!session) {
    const created = await api.createSession();
    api.setSession(created.session_id);
    session = { messages: [] };
  }
  appendHistory(session.messages);
  clearImageState();
  imageStateLoading.value = false;
  imageStateLoaded.value = false;
  imageStateError.value = "";
  authenticated.value = true;
  void nextTick(resizeComposer);
  connectEvents();
  void loadImageState();
  void ensureVoiceCatalog();
}

async function bootstrap(): Promise<void> {
  booting.value = true;
  try {
    await api.checkHealth();
    await openSession();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      api.setToken("");
      setConnection("需要登录", "neutral");
    } else {
      setConnection("Gateway 不可用", "error");
      loginError.value = error instanceof Error ? error.message : "无法连接 Gateway";
    }
  } finally {
    booting.value = false;
  }
}

async function login(): Promise<void> {
  loginError.value = "";
  const token = tokenInput.value.trim();
  try {
    await api.checkHealth(undefined, token);
    api.setToken(token);
    api.clearSession();
    await openSession();
  } catch (error) {
    loginError.value = error instanceof Error ? error.message : "登录失败";
  }
}

function logout(): void {
  closeEvents();
  clearTimeline(true);
  api.logout();
  authenticated.value = false;
  clearMessages();
  clearImageState();
  imageStateLoading.value = false;
  imageStateLoaded.value = false;
  imageStateError.value = "";
  activeView.value = "chat";
  autoFollowMessages.value = true;
  showScrollToLatest.value = false;
  previewUrl.value = "";
  pendingAssistantId.value = null;
  autoplayRequests.clear();
  clearSpeechState();
  clearVoiceCatalog();
  setConnection("已退出", "neutral");
}

async function sendMessage(): Promise<void> {
  hideModeSuggestions();
  const content = draft.value.trim();
  if (!content || sending.value || !api.sessionId) return;
  const previousAgentId = latestMessage("agent")?.id || null;
  // A POST fallback may retain its id to absorb a late SSE completion. Once a
  // new turn starts, that id belongs to the previous turn and must not be
  // reused by the next assistant.started event.
  pendingAssistantId.value = null;
  appendMessage(makeMessage("user", content));
  draft.value = "";
  void nextTick(resizeComposer);
  sending.value = true;
  taskStatus.value = "正在发送…";
  scrollToLatest(true);
  try {
    const response = await api.sendMessage(api.sessionId, content);
    // SSE normally supplies the completed bubble. This fallback keeps the turn
    // visible if a mobile network drops the event right as POST returns.
    let current = pendingAssistant();
    const latest = latestMessage("agent");
    if (!current && latest && latest.id !== previousAgentId) current = latest;
    if (current) {
      current.text = response.answer;
      current.status = response.ok ? "done" : "error";
      current.meta.finishedAt = Date.now();
      if (current.status === "done" && current.id === pendingAssistantId.value) {
        // Keep the id briefly so a late assistant.started event can reuse this
        // fallback bubble instead of creating a duplicate after a mobile race.
        pendingAssistantId.value = current.id;
      }
      maybeAutoplay(current);
    } else {
      const fallback = appendMessage(makeMessage("agent", response.answer, response.ok ? "done" : "error"));
      pendingAssistantId.value = fallback.id;
      maybeAutoplay(fallback);
    }
  } catch (error) {
    // The Gateway publishes agent.error before returning HTTP 500. Mirror the
    // legacy client and avoid rendering the same failure from both channels.
    if (Date.now() - lastAgentErrorAt > 1000) {
      finishAssistant(error instanceof Error ? error.message : "发送失败", "error");
    }
  } finally {
    sending.value = false;
    taskStatus.value = "";
    scrollToLatest();
  }
}

function handleComposerKeydown(event: KeyboardEvent): void {
  if (event.isComposing) return;
  if (modeSuggestionsOpen.value && modeSuggestions.value.length) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      activateModeSuggestion(modeSuggestionIndex.value + 1);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      activateModeSuggestion(modeSuggestionIndex.value - 1);
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      const selected = modeSuggestions.value[modeSuggestionIndex.value];
      if (selected) selectModeSuggestion(selected);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      hideModeSuggestions();
      return;
    }
  }
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    void sendMessage();
  }
}

onMounted(() => void bootstrap());
onBeforeUnmount(() => {
  closeEvents();
  clearTimeline(true);
  if (modeBlurTimer !== null) {
    window.clearTimeout(modeBlurTimer);
    modeBlurTimer = null;
  }
  modeSuggestionRequestId += 1;
  if (composerInput.value) {
    composerInput.value.style.height = "";
    composerInput.value.style.overflowY = "";
  }
  clearSpeechState();
});
</script>

<template>
  <main class="chat-shell">
    <section v-if="!authenticated" class="login-card" aria-labelledby="login-title">
      <p class="eyebrow">LEON AGENT · VUE 3</p>
      <h1 id="login-title">登录聊天工作台</h1>
      <p class="subtitle">Vue 迁移入口默认不替换 legacy 页面；登录后会恢复当前会话。</p>
      <form class="login-form" @submit.prevent="login">
        <label for="token">Gateway Token</label>
        <input id="token" v-model="tokenInput" type="password" autocomplete="current-password" placeholder="公网部署时填写 LEON_API_TOKEN" />
        <button type="submit" :disabled="booting || !tokenInput.trim()">进入 Leon</button>
      </form>
      <p v-if="loginError" class="form-error">{{ loginError }}</p>
      <p v-else-if="booting" class="form-hint">正在检查 Gateway…</p>
    </section>

    <section v-else class="chat-app" aria-label="Leon Agent 工作台">
      <header class="chat-header">
        <div>
          <p class="eyebrow">LEON AGENT · VUE 3</p>
          <h1>Leon Agent</h1>
        </div>
        <div class="header-actions">
          <button
            class="ghost-button timeline-toggle"
            type="button"
            aria-controls="timeline-panel"
            :aria-expanded="timelineOpen"
            @click="toggleTimeline"
          >
            时间线
          </button>
          <AppStatus :label="connectionLabel" :tone="connectionTone" />
          <button class="ghost-button" type="button" @click="logout">退出</button>
        </div>
      </header>

      <aside
        v-if="timelineOpen"
        id="timeline-panel"
        ref="timelinePanel"
        class="timeline-panel"
        tabindex="-1"
        role="dialog"
        aria-modal="false"
        aria-label="Agent Timeline"
        @keydown.escape="closeTimeline"
      >
        <div class="timeline-panel__header">
          <div>
            <h2>Agent Timeline</h2>
            <p>最近的 SSE 决策事件</p>
          </div>
          <div class="timeline-panel__actions">
            <button type="button" @click="clearTimeline()">清空</button>
            <button type="button" aria-label="关闭时间线" @click="closeTimeline">×</button>
          </div>
        </div>
        <ol v-if="timelineEntries.length" class="timeline-list">
          <li
            v-for="entry in timelineEntries"
            :key="entry.id"
            class="timeline-entry"
            :data-kind="entry.kind"
          >
            <span class="timeline-entry__dot" aria-hidden="true"></span>
            <div class="timeline-entry__body">
              <div class="timeline-entry__title">
                <strong>{{ entry.label }}</strong>
                <time>{{ entry.time }}</time>
              </div>
              <small>{{ entry.event }}</small>
              <pre v-if="entry.detail" class="timeline-entry__detail">{{ entry.detail }}</pre>
            </div>
          </li>
        </ol>
        <p v-else class="timeline-empty">暂无事件</p>
      </aside>

      <section v-if="activeView === 'chat'" class="chat-view" aria-label="聊天">
        <div class="messages-stage">
          <div
            ref="messagesPanel"
            class="messages-panel"
            aria-live="polite"
            @scroll="handleMessagesScroll"
          >
            <div v-if="!messages.length" class="empty-state">
              <strong>开始一段对话</strong>
              <span>普通聊天和生图请求都会沿用现有 Gateway 协议。</span>
            </div>
            <MessageBubble
              v-for="message in messages"
              :key="message.id"
              :message="message"
              @retry="retryMessage"
              @edit="editMessage"
              @preview="openPreview"
              @media-loaded="scrollToLatest"
            />
          </div>
          <button
            v-if="showScrollToLatest"
            class="scroll-to-latest"
            type="button"
            aria-label="回到最新消息"
            @click="scrollToLatest(true)"
          >
            ↓ 回到最新
          </button>
        </div>

        <p v-if="taskStatus" class="task-status">{{ taskStatus }}</p>
        <form class="composer" @submit.prevent="sendMessage">
          <div class="composer-wrap">
            <div
              v-if="modeSuggestionsOpen"
              ref="modeSuggestionList"
              class="mode-suggestions"
              role="listbox"
              aria-label="生图模式"
            >
              <button
                v-for="(item, index) in modeSuggestions"
                :key="item.id"
                class="mode-suggestion"
                :class="{ active: index === modeSuggestionIndex }"
                type="button"
                role="option"
                :aria-selected="index === modeSuggestionIndex"
                @mousedown.prevent
                @mouseenter="activateModeSuggestion(index)"
                @click="selectModeSuggestion(item)"
              >
                <span class="mode-suggestion-name">{{ item.name }}</span>
                <span class="mode-suggestion-id">{{ item.id }}</span>
              </button>
            </div>
            <textarea
              ref="composerInput"
              v-model="draft"
              rows="1"
              maxlength="8000"
              placeholder="输入消息，Enter 发送，Shift+Enter 换行"
              :disabled="sending"
              @keydown="handleComposerKeydown"
              @input="handleComposerInput"
              @focus="handleComposerFocus"
              @blur="scheduleHideModeSuggestions"
            ></textarea>
          </div>
          <button type="submit" :disabled="sending || !draft.trim()">
            {{ sending ? "发送中…" : "发送" }}
          </button>
        </form>
      </section>

      <TasksView
        v-else-if="activeView === 'tasks'"
        :loading="imageStateLoading"
        :error="imageStateError"
        @refresh="refreshImageState"
      />
      <GalleryView
        v-else-if="activeView === 'gallery'"
        :loading="imageStateLoading"
        :error="imageStateError"
        @refresh="refreshImageState"
      />
      <SettingsView v-else :session-id="api.sessionId" />

      <BottomNav :active="activeView" @select="selectView" />

      <button
        v-if="audioUnlockRequired"
        class="audio-unlock"
        type="button"
        @click="void unlockAudio()"
      >
        🔈 点击开启语音播放
      </button>

      <div
        v-if="previewUrl"
        ref="previewDialog"
        class="image-viewer"
        tabindex="0"
        role="dialog"
        aria-modal="true"
        aria-label="图片预览"
        @click.self="closePreview"
        @keydown.escape="closePreview"
      >
        <button class="image-viewer__close" type="button" aria-label="关闭" @click="closePreview">
          ×
        </button>
        <figure class="image-viewer__figure">
          <img :src="previewUrl" alt="图片预览" />
        </figure>
      </div>
    </section>
  </main>
</template>
