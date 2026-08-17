<script setup lang="ts">
import { ArrowDown, History, LoaderCircle, Mic, RefreshCw, Send, Sparkles, Square } from "@lucide/vue";
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import AppStatus from "../components/AppStatus.vue";
import BottomNav, { type WorkbenchView } from "../components/BottomNav.vue";
import ImageViewer, { type ViewerImage } from "../components/ImageViewer.vue";
import MessageEditDialog from "../components/MessageEditDialog.vue";
import MessageBubble from "../components/MessageBubble.vue";
import {
  ApiError,
  api,
  type ImageMode,
  type LeonEvent,
  type SessionMessage,
  type SessionResponse,
} from "../api/client";
import GalleryView from "./GalleryView.vue";
import SettingsView from "./SettingsView.vue";
import TasksView from "./TasksView.vue";
import {
  appendMessage,
  beginMessageRevision,
  clearMessages,
  cycleMessageRevision,
  findMessage,
  hasVoiceClip,
  insertMessageBefore,
  latestMessage,
  makeMessage,
  messages,
  removeMessage,
  type ChatMessage,
  type MessageRevision,
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
  speakMessage,
  unlockAudio,
} from "../utils/speech";
import { extractImageHrefs, safeHref, stripImageLinks } from "../utils/markdown";

const authenticated = ref(false);
const booting = ref(true);
const tokenInput = ref("");
const loginError = ref("");
const draft = ref("");
const sending = ref(false);
const connectionLabel = ref("未连接");
const connectionTone = ref<"neutral" | "ok" | "error">("neutral");
const activeView = ref<WorkbenchView>("chat");
const imageStateLoading = ref(false);
const imageStateLoaded = ref(false);
const imageStateError = ref("");
const previewItems = ref<ViewerImage[]>([]);
const previewIndex = ref(0);
const editingMessageId = ref<string | null>(null);
const editDraft = ref("");
const messagesPanel = ref<HTMLElement | null>(null);
const composerInput = ref<HTMLTextAreaElement | null>(null);
const modeSuggestionList = ref<HTMLElement | null>(null);
const timelinePanel = ref<HTMLElement | null>(null);
const timelineOpen = ref(false);
const timelineEntries = ref<TimelineEntry[]>([]);
const autoFollowMessages = ref(true);
const showScrollToLatest = ref(false);
const pendingAssistantId = ref<string | null>(null);
const pendingImageResultId = ref<string | null>(null);
const modeSuggestions = ref<ImageMode[]>([]);
const modeSuggestionIndex = ref(0);
const modeSuggestionsOpen = ref(false);
let eventSource: EventSource | null = null;
let reconnectTimer: number | null = null;
let eventHealthProbeSource: EventSource | null = null;
let modeBlurTimer: number | null = null;
let imageModeCatalog: ImageMode[] | null = null;
let imageModeCatalogRequest: Promise<ImageMode[]> | null = null;
let modeSuggestionRequestId = 0;
let lastAgentErrorFingerprint = "";
let lastAgentErrorAt = 0;
let activeSendController: AbortController | null = null;
let suppressedUserEcho: { content: string; until: number } | null = null;
const autoplayRequests = new Set<string>();
const COMPOSER_MIN_HEIGHT = 42;
const COMPOSER_MAX_HEIGHT = 120;
const SCROLL_FOLLOW_THRESHOLD = 72;
const MAX_TIMELINE_ENTRIES = 100;
const ERROR_DEDUPE_WINDOW_MS = 10_000;
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
    "assistant.cancelled": "已停止",
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
  const labels: Record<string, string> = {
    content: "内容",
    error: "错误",
    tool_name: "工具",
    mode_name: "模式",
    mode_id: "模式编号",
    status: "状态",
    voice_name: "音色",
    job_id: "任务编号",
  };
  const preferred = Object.keys(labels);
  const parts = preferred
    .filter((key) => data[key] !== undefined && data[key] !== null && data[key] !== "")
    .map((key) => `${labels[key]}：${timelineValue(data[key])}`);
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

const asrAvailable = ref(false);
const asrState = ref<"idle" | "recording" | "uploading">("idle");
const asrNotice = ref("");
let asrRecorder: MediaRecorder | null = null;
let asrChunks: BlobPart[] = [];
let asrNoticeTimer: number | null = null;

function showAsrNotice(message: string): void {
  asrNotice.value = message;
  if (asrNoticeTimer !== null) window.clearTimeout(asrNoticeTimer);
  asrNoticeTimer = window.setTimeout(() => {
    asrNotice.value = "";
    asrNoticeTimer = null;
  }, 4000);
}

async function probeAsrAvailability(): Promise<void> {
  try {
    asrAvailable.value = (await api.getAsrStatus()).enabled;
  } catch {
    asrAvailable.value = false;
  }
}

function stopAsrRecording(): void {
  if (asrRecorder && asrRecorder.state !== "inactive") asrRecorder.stop();
  else asrState.value = "idle";
}

async function toggleVoiceInput(): Promise<void> {
  if (asrState.value === "recording") {
    stopAsrRecording();
    return;
  }
  if (asrState.value !== "idle") return;
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    showAsrNotice("当前浏览器不支持语音录入");
    return;
  }
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (error) {
    showAsrNotice(`麦克风不可用：${(error as Error).message || error}`);
    return;
  }
  const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
  const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  asrChunks = [];
  asrRecorder = recorder;
  recorder.ondataavailable = (event) => {
    if (event.data.size > 0) asrChunks.push(event.data);
  };
  recorder.onstop = () => {
    stream.getTracks().forEach((track) => track.stop());
    void submitAsrRecording(recorder);
  };
  asrState.value = "recording";
  recorder.start();
}

async function submitAsrRecording(recorder: MediaRecorder): Promise<void> {
  const blob = new Blob(asrChunks, { type: recorder.mimeType || "audio/webm" });
  asrChunks = [];
  if (asrRecorder === recorder) asrRecorder = null;
  if (!blob.size) {
    asrState.value = "idle";
    return;
  }
  asrState.value = "uploading";
  try {
    const text = await api.transcribeAudio(blob);
    if (text) {
      draft.value = draft.value ? `${draft.value.trimEnd()} ${text}` : text;
      await nextTick();
      resizeComposer();
      composerInput.value?.focus();
    }
  } catch (error) {
    showAsrNotice(`语音识别失败：${(error as Error).message || error}`);
  } finally {
    asrState.value = "idle";
  }
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
  const selected = findMessage(messageId);
  if (!selected) return;
  const index = messages.value.findIndex((message) => message.id === messageId);
  let content = "";
  let target: ChatMessage | null = null;
  if (selected.role === "user") {
    content = selected.text.trim();
    for (let cursor = index + 1; cursor < messages.value.length; cursor += 1) {
      if (messages.value[cursor].role === "agent") {
        target = messages.value[cursor];
        break;
      }
    }
  } else if (selected.role === "agent") {
    target = selected;
    for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
      const candidate = messages.value[cursor];
      if (candidate.role !== "user" || !candidate.text.trim()) continue;
      content = candidate.text.trim();
      break;
    }
  }
  if (!content) return;

  const retryLatest = target?.id === latestMessage("agent")?.id;
  if (!target) target = appendMessage(makeMessage("agent", "", "pending"));
  else beginMessageRevision(target);
  pendingAssistantId.value = target.id;
  suppressedUserEcho = { content, until: Date.now() + ERROR_DEDUPE_WINDOW_MS };
  void sendTurn(content, { appendUser: false, retry: retryLatest });
}

function cycleRevision(messageId: string): void {
  const message = findMessage(messageId);
  if (message) cycleMessageRevision(message);
}

function openMessageEditor(messageId: string): void {
  const message = findMessage(messageId);
  if (!message) return;
  editingMessageId.value = messageId;
  editDraft.value = message.text;
}

function closeMessageEditor(): void {
  editingMessageId.value = null;
  editDraft.value = "";
}

function saveMessageEditor(): void {
  const message = findMessage(editingMessageId.value);
  if (message) message.text = editDraft.value;
  closeMessageEditor();
}

function openPreview(urls: string[] | string, index = 0): void {
  const values = Array.isArray(urls) ? urls : [urls];
  const unique = [...new Set(values.map((url) => url.trim()).filter(Boolean))];
  previewItems.value = unique.map((url) => ({ url }));
  previewIndex.value = Math.min(Math.max(index, 0), Math.max(unique.length - 1, 0));
}

function closePreview(): void {
  previewItems.value = [];
  previewIndex.value = 0;
}

function closeEvents(): void {
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  eventHealthProbeSource = null;
  eventSource?.close();
  eventSource = null;
}

function scheduleClosedEventReconnect(source: EventSource): void {
  if (reconnectTimer !== null || source !== eventSource || !authenticated.value) return;
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    if (source !== eventSource || !authenticated.value) return;
    source.close();
    eventSource = null;
    connectEvents();
  }, 3000);
}

function expireLogin(): void {
  logout();
  tokenInput.value = "";
  loginError.value = "登录已失效，请重新登录";
  setConnection("需要登录", "neutral");
}

function handleEventError(source: EventSource): void {
  if (source !== eventSource) return;
  setConnection(source.readyState === EventSource.CONNECTING ? "正在重连…" : "连接断开", "error");

  // EventSource natively retries transient network failures. Probe auth once
  // instead of closing it here; a 204 stale-token response leaves the source
  // permanently CLOSED and must return the UI to the login screen.
  if (eventHealthProbeSource === source) return;
  eventHealthProbeSource = source;
  const probeAbort = new AbortController();
  const probeTimeout = window.setTimeout(() => probeAbort.abort(), 5000);
  void api
    .checkHealth(probeAbort.signal)
    .then(() => {
      if (source === eventSource && source.readyState === EventSource.CLOSED) {
        scheduleClosedEventReconnect(source);
      }
    })
    .catch((error: unknown) => {
      if (source !== eventSource) return;
      if (error instanceof ApiError && error.status === 401) {
        expireLogin();
      } else if (source.readyState === EventSource.CLOSED) {
        scheduleClosedEventReconnect(source);
      }
    })
    .finally(() => {
      window.clearTimeout(probeTimeout);
      if (eventHealthProbeSource === source) eventHealthProbeSource = null;
    });
}

function handleOnline(): void {
  if (!authenticated.value || !api.sessionId) return;
  if (!eventSource || eventSource.readyState === EventSource.CLOSED) connectEvents();
}

function hasImageContent(content: string): boolean {
  return extractImageHrefs(content).length > 0;
}

function isImageOnlyContent(content: string): boolean {
  return !stripImageLinks(content).trim();
}

function appendHistory(
  history: SessionMessage[],
): void {
  clearMessages();
  for (let index = 0; index < history.length; index += 1) {
    const item = history[index];
    if (item.role !== "user" && item.role !== "assistant") continue;
    let content = item.content;
    const next = history[index + 1];
    if (
      item.role === "assistant" &&
      isImageOnlyContent(content) &&
      next?.role === "assistant" &&
      next.content.trim() &&
      !hasImageContent(next.content)
    ) {
      content = `${content}\n\n${next.content}`;
      index += 1;
    }
    const message = appendMessage(
      makeMessage(item.role === "assistant" ? "agent" : "user", content),
    );
    if (typeof item.id === "number") message.id = `db_${item.id}`;
    if (item.role === "assistant") {
      message.images = extractImageHrefs(content);
      if (message.images.length) message.kind = "image-result";
    }
    if (item.role === "assistant" && Array.isArray(item.revisions)) {
      message.revisions = item.revisions.map((revision): MessageRevision => ({
        kind: hasImageContent(revision.content) ? "image-result" : "message",
        text: revision.content,
        status: "done",
        images: extractImageHrefs(revision.content),
        tools: [],
        meta: {
          model: null,
          startedAt: null,
          finishedAt: revision.created_at || null,
          elapsedMs: null,
          tokensIn: null,
          tokensOut: null,
        },
      }));
      message.revisionIndex = message.revisions.length;
    }
    message.createdAt =
      typeof item.created_at === "number" && item.created_at > 0 ? item.created_at : null;
  }
  scrollToLatest(true);
}

const TIME_DIVIDER_GAP_MS = 10 * 60 * 1000;

function messageTimeLabel(timestamp: number): string {
  const date = new Date(timestamp);
  const now = new Date();
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();
  const clock = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  return sameDay ? clock : `${date.getMonth() + 1}月${date.getDate()}日 ${clock}`;
}

function showTimeDivider(index: number): boolean {
  const current = messages.value[index];
  if (!current.createdAt) return false;
  if (index === 0) return true;
  const previous = messages.value[index - 1];
  if (!previous.createdAt) return false;
  return current.createdAt - previous.createdAt > TIME_DIVIDER_GAP_MS;
}

function pendingAssistant(): ChatMessage | null {
  return findMessage(pendingAssistantId.value);
}

function cancelPendingAssistant(): void {
  const message = pendingAssistant();
  pendingAssistantId.value = null;
  if (!message) return;
  const hasVisibleWork = Boolean(
    message.text.trim() || message.tools.length || message.images.length || message.revisions.length,
  );
  if (!hasVisibleWork) {
    removeMessage(message.id);
    return;
  }
  message.status = "cancelled";
  message.tools.forEach((tool) => {
    if (tool.status === "running") tool.status = "cancelled";
  });
  message.meta.finishedAt = Date.now();
  message.meta.elapsedMs = message.meta.startedAt
    ? message.meta.finishedAt - message.meta.startedAt
    : null;
  if (!activeSendController) sending.value = false;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function toolOutputImageUrls(output: Record<string, unknown>): string[] {
  const rows = [output];
  for (const key of ["items", "images"]) {
    const value = output[key];
    if (Array.isArray(value)) {
      rows.push(...value.map(asRecord));
    }
  }
  const urls: string[] = [];
  for (const row of rows) {
    const raw = asString(
      row.image_url ?? row.imageUrl ?? row.final_image_url ?? row.finalImageUrl,
    );
    const url = safeHref(raw);
    if (url && !urls.includes(url)) urls.push(url);
  }
  return urls;
}

function startTool(data: Record<string, unknown>): void {
  const message = pendingAssistant() || appendMessage(makeMessage("agent", "", "pending"));
  message.status = message.text ? "streaming" : "pending";
  message.meta.startedAt ||= Date.now();
  message.tools.push({
    id: `tool_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
    name: asString(data.tool_name) || "tool",
    status: "running",
    input: asRecord(data.input),
    output: {},
  });
  pendingAssistantId.value = message.id;
  scrollToLatest();
}

function finishTool(data: Record<string, unknown>): void {
  const message = pendingAssistant();
  if (!message) return;
  const name = asString(data.tool_name) || "tool";
  let tool = [...message.tools]
    .reverse()
    .find((item) => item.name === name && item.status === "running");
  if (!tool) {
    tool = {
      id: `tool_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
      name,
      status: "running",
      input: {},
      output: {},
    };
    message.tools.push(tool);
  }
  tool.status = data.ok === false || asString(data.ok) === "false" ? "error" : "done";
  tool.output = asRecord(data.output);
  for (const url of toolOutputImageUrls(tool.output)) {
    if (!message.images.includes(url)) message.images.push(url);
  }
  if (message.images.length) message.kind = "image-result";
  scrollToLatest();
}

function ensureVoiceCatalog(): Promise<boolean> {
  return loadVoiceCatalog().catch(() => false);
}

function maybeAutoplay(message: ChatMessage): void {
  if (
    message.role !== "agent" ||
    message.status !== "done" ||
    !message.text.trim() ||
    message.kind === "image-result" ||
    message.images.length > 0 ||
    hasImageContent(message.text) ||
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

interface CompletedMeta {
  model?: string | null;
  elapsedMs?: number | null;
  tokensIn?: number | null;
  tokensOut?: number | null;
}

function finishAssistant(
  content: string,
  status: "done" | "error" = "done",
  serverMeta: CompletedMeta = {},
): void {
  let completed = pendingAssistant();
  if (completed) {
    completed.text = content;
    completed.status = status;
    completed.meta.finishedAt = Date.now();
    const clientElapsed = completed.meta.startedAt
      ? completed.meta.finishedAt - completed.meta.startedAt
      : null;
    completed.meta.elapsedMs =
      typeof serverMeta.elapsedMs === "number" && serverMeta.elapsedMs >= 0
        ? serverMeta.elapsedMs
        : clientElapsed;
  } else if (content) {
    completed = appendMessage(makeMessage("agent", content, status));
    if (typeof serverMeta.elapsedMs === "number" && serverMeta.elapsedMs >= 0) {
      completed.meta.elapsedMs = serverMeta.elapsedMs;
    }
  }
  if (completed) {
    // Keep structured tool images authoritative. Assistant prose often repeats
    // the same URLs, and merging both sources can create duplicate cards.
    if (!completed.images.length) {
      for (const url of extractImageHrefs(content)) {
        if (!completed.images.includes(url)) completed.images.push(url);
      }
    }
    if (completed.images.length) completed.kind = "image-result";
    if (serverMeta.model) completed.meta.model = serverMeta.model;
    if (typeof serverMeta.tokensIn === "number") completed.meta.tokensIn = serverMeta.tokensIn;
    if (typeof serverMeta.tokensOut === "number") completed.meta.tokensOut = serverMeta.tokensOut;
  }
  pendingAssistantId.value = null;
  if (!activeSendController) sending.value = false;
  if (completed) maybeAutoplay(completed);
  scrollToLatest();
}

function errorFingerprint(content: string): string {
  return content.trim().toLocaleLowerCase().replace(/\s+/g, " ");
}

function finishAgentErrorOnce(content: string): void {
  const resolved = content.trim() || "请求失败";
  const fingerprint = errorFingerprint(resolved);
  const now = Date.now();
  const latest = latestMessage("agent");
  if (
    (fingerprint === lastAgentErrorFingerprint && now - lastAgentErrorAt < ERROR_DEDUPE_WINDOW_MS) ||
    (latest?.status === "error" && errorFingerprint(latest.text) === fingerprint)
  ) {
    return;
  }
  lastAgentErrorFingerprint = fingerprint;
  lastAgentErrorAt = now;
  finishAssistant(resolved, "error");
}

function parseCompletedMeta(data: Record<string, unknown>): CompletedMeta {
  const usage = (data.usage && typeof data.usage === "object" ? data.usage : {}) as Record<
    string,
    unknown
  >;
  const tokensIn = Number(usage.input_tokens);
  const tokensOut = Number(usage.output_tokens);
  const elapsedMs = Number(data.elapsed_ms);
  return {
    model: asString(data.model).trim() || null,
    elapsedMs: Number.isFinite(elapsedMs) && elapsedMs >= 0 ? elapsedMs : null,
    tokensIn: Number.isFinite(tokensIn) && tokensIn > 0 ? tokensIn : null,
    tokensOut: Number.isFinite(tokensOut) && tokensOut > 0 ? tokensOut : null,
  };
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
}

function handleEvent(event: LeonEvent): void {
  const data = event.data || {};
  recordTimelineEvent(event);
  switch (event.event) {
    case "session.connected":
      setConnection("已连接", "ok");
      break;
    case "user.message": {
      const content = asString(data.content);
      if (
        content &&
        suppressedUserEcho?.content === content &&
        suppressedUserEcho.until >= Date.now()
      ) {
        suppressedUserEcho = null;
        break;
      }
      const last = latestMessage("user");
      if (content && last?.text !== content) appendMessage(makeMessage("user", content));
      break;
    }
    case "assistant.started": {
      const message = pendingAssistant() || appendMessage(makeMessage("agent", "", "pending"));
      message.status = "pending";
      message.meta.startedAt = Date.now();
      pendingAssistantId.value = message.id;
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
      scrollToLatest();
      break;
    }
    case "assistant.completed":
      finishAssistant(
        asString(data.content) || pendingAssistant()?.text || "",
        "done",
        parseCompletedMeta(data),
      );
      break;
    case "assistant.cancelled":
      cancelPendingAssistant();
      scrollToLatest();
      break;
    case "assistant.notice": {
      const content = asString(data.content);
      const jobIds = Array.isArray(data.job_ids) ? data.job_ids : [];
      const imageResult = jobIds.length ? findMessage(pendingImageResultId.value) : null;
      if (content && imageResult?.kind === "image-result") {
        imageResult.text = content;
        imageResult.status = "done";
        pendingImageResultId.value = null;
        maybeAutoplay(imageResult);
      } else if (content) {
        appendMessage(makeMessage("agent", content));
      }
      scrollToLatest();
      break;
    }
    case "agent.error":
      finishAgentErrorOnce(asString(data.error) || "请求失败");
      break;
    case "tool.started":
      startTool(data);
      break;
    case "tool.finished":
      finishTool(data);
      break;
    case "image.task.created":
      upsertImageTask({ ...data, created_at: eventTime(event.timestamp) });
      break;
    case "image.task.updated":
      upsertImageTask(data);
      break;
    case "image.completed": {
      const imageUrl = asString(data.image_url);
      if (!imageUrl) break;
      const jobId = asString(data.job_id);
      if (jobId) completeImageTask(jobId, imageUrl, eventTime(event.timestamp));
      if (!messages.value.some((message) => message.images.includes(imageUrl))) {
        let target = findMessage(pendingImageResultId.value);
        if (!target || target.kind !== "image-result") {
          target = appendMessage(makeMessage("agent"));
          target.kind = "image-result";
          pendingImageResultId.value = target.id;
        }
        target.images.push(imageUrl);
      }
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
    handleEventError,
  );
}

function restoreActiveTurn(activeTurn: { retry: boolean } | null): void {
  if (!activeTurn) return;
  let target: ChatMessage | null = null;
  if (activeTurn.retry) {
    target = latestMessage("agent");
    if (target) beginMessageRevision(target);
  }
  if (!target) target = appendMessage(makeMessage("agent", "", "pending"));
  target.status = "pending";
  target.meta.startedAt ||= Date.now();
  pendingAssistantId.value = target.id;
  sending.value = true;
  scrollToLatest(true);
}

async function reconcileActiveTurn(sessionId: string): Promise<void> {
  try {
    const refreshed = await api.getSession(sessionId);
    if (api.sessionId !== sessionId) return;
    if (refreshed.active_turn) {
      if (!pendingAssistant()) restoreActiveTurn(refreshed.active_turn);
      return;
    }
    if (sending.value && !activeSendController) {
      pendingAssistantId.value = null;
      sending.value = false;
      appendHistory(refreshed.messages);
    }
  } catch {
    // SSE remains authoritative; the next explicit refresh can retry hydration.
  }
}

async function openSession(): Promise<void> {
  clearTimeline(true);
  let session: SessionResponse | null = null;
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
    session = { session_id: created.session_id, messages: [], active_turn: null };
  }
  appendHistory(session.messages);
  restoreActiveTurn(session.active_turn);
  clearImageState();
  imageStateLoading.value = false;
  imageStateLoaded.value = false;
  imageStateError.value = "";
  authenticated.value = true;
  void nextTick(resizeComposer);
  connectEvents();
  if (session.active_turn) void reconcileActiveTurn(api.sessionId);
  void loadImageState();
  void ensureVoiceCatalog();
  void probeAsrAvailability();
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
      setConnection("服务不可用", "error");
      loginError.value = error instanceof Error ? error.message : "无法连接服务";
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
  closePreview();
  pendingAssistantId.value = null;
  pendingImageResultId.value = null;
  activeSendController?.abort();
  activeSendController = null;
  sending.value = false;
  suppressedUserEcho = null;
  autoplayRequests.clear();
  clearSpeechState();
  clearVoiceCatalog();
  setConnection("已退出", "neutral");
}

interface SendTurnOptions {
  appendUser: boolean;
  retry: boolean;
}

async function sendTurn(content: string, options: SendTurnOptions): Promise<void> {
  const sessionId = api.sessionId;
  if (!content || sending.value || !sessionId) return;
  const previousAgentId = latestMessage("agent")?.id || null;
  // A POST fallback may retain its id to absorb a late SSE completion. Once a
  // new turn starts, that id belongs to the previous turn and must not be
  // reused by the next assistant.started event.
  if (options.appendUser) {
    pendingAssistantId.value = null;
    suppressedUserEcho = null;
    appendMessage(makeMessage("user", content));
    draft.value = "";
    void nextTick(resizeComposer);
  }
  // Retry reuses an existing bubble. Keep that exact identity even if SSE
  // completes first and clears pendingAssistantId before the POST resolves.
  const turnAssistantId = pendingAssistantId.value;
  const controller = new AbortController();
  activeSendController = controller;
  sending.value = true;
  scrollToLatest(true);
  try {
    const response = await api.sendMessage(sessionId, content, {
      signal: controller.signal,
      retry: options.retry,
    });
    // SSE normally supplies the completed bubble. This fallback keeps the turn
    // visible if a mobile network drops the event right as POST returns.
    let current = pendingAssistant();
    if (!current && turnAssistantId) current = findMessage(turnAssistantId);
    const latest = latestMessage("agent");
    if (!current && latest && latest.id !== previousAgentId) current = latest;
    if (current) {
      current.text = response.answer;
      current.status = response.ok ? "done" : response.answer === "已停止" ? "cancelled" : "error";
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
    if (!controller.signal.aborted) {
      finishAgentErrorOnce(error instanceof Error ? error.message : "发送失败");
    }
  } finally {
    if (activeSendController === controller) {
      activeSendController = null;
      sending.value = false;
    }
    scrollToLatest();
  }
}

async function sendMessage(): Promise<void> {
  hideModeSuggestions();
  const content = draft.value.trim();
  await sendTurn(content, { appendUser: true, retry: false });
}

async function stopSending(): Promise<void> {
  const sessionId = api.sessionId;
  if (!sending.value || !sessionId) return;
  const cancelRequest = api.cancelMessage(sessionId);
  activeSendController?.abort();
  sending.value = false;
  cancelPendingAssistant();
  scrollToLatest();
  try {
    await cancelRequest;
  } catch (error) {
    showAsrNotice(`停止请求失败：${error instanceof Error ? error.message : "未知错误"}`);
  }
}

function handleSendButton(): void {
  if (sending.value) void stopSending();
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

onMounted(() => {
  window.addEventListener("online", handleOnline);
  void bootstrap();
});
onBeforeUnmount(() => {
  closeEvents();
  window.removeEventListener("online", handleOnline);
  clearTimeline(true);
  stopAsrRecording();
  if (asrNoticeTimer !== null) {
    window.clearTimeout(asrNoticeTimer);
    asrNoticeTimer = null;
  }
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
    <section v-if="!authenticated" class="login-view" aria-labelledby="login-title">
      <div class="login-card">
        <div class="login-brand">
          <span class="login-brand__mark" aria-hidden="true">
            <Sparkles :size="30" :stroke-width="1.9" />
          </span>
          <div>
            <div class="login-brand__wordmark">
              <h1 id="login-title">Leon</h1>
              <span>AGENT</span>
            </div>
            <p>聊天 语音 生图  Have Fun 😈</p>
          </div>
        </div>
        <form class="login-form" @submit.prevent="login">
          <label for="token">访问口令</label>
          <input
            id="token"
            v-model="tokenInput"
            type="password"
            autocomplete="current-password"
            placeholder="输入访问口令"
          />
          <button type="submit" :disabled="booting || !tokenInput.trim()">连接</button>
          <p v-if="loginError" class="form-error">{{ loginError }}</p>
        </form>
      </div>
    </section>

    <section v-else class="chat-app" aria-label="Leon 工作台">
      <header class="chat-header">
        <div class="chat-header__brand">
          <Sparkles class="chat-header__spark" :size="20" :stroke-width="2" aria-hidden="true" />
          <div class="chat-header__identity">
            <div class="chat-header__wordmark">
              <h1>Leon</h1>
              <span>AGENT</span>
            </div>
            <AppStatus :label="connectionLabel" :tone="connectionTone" />
          </div>
        </div>
        <div class="header-actions">
          <button
            v-if="activeView === 'tasks' || activeView === 'gallery'"
            class="header-icon-button"
            type="button"
            :disabled="imageStateLoading"
            :aria-label="activeView === 'tasks' ? '刷新任务' : '刷新图库'"
            :title="activeView === 'tasks' ? '刷新任务' : '刷新图库'"
            @click="refreshImageState"
          >
            <RefreshCw :class="{ spinning: imageStateLoading }" :size="18" :stroke-width="2" aria-hidden="true" />
          </button>
          <button
            class="header-icon-button timeline-toggle"
            type="button"
            aria-controls="timeline-panel"
            :aria-expanded="timelineOpen"
            aria-label="运行记录"
            title="运行记录"
            @click="toggleTimeline"
          >
            <History :size="18" :stroke-width="2" aria-hidden="true" />
          </button>
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
        aria-label="运行记录"
        @keydown.escape="closeTimeline"
      >
        <div class="timeline-panel__header">
          <div>
            <h2>运行记录</h2>
            <p>最近的运行事件</p>
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
              <span>聊天、生图，都可以。</span>
            </div>
            <template v-for="(message, index) in messages" :key="message.id">
              <p v-if="showTimeDivider(index)" class="time-divider" role="time">
                {{ messageTimeLabel(message.createdAt!) }}
              </p>
              <MessageBubble
                :message="message"
                @retry="retryMessage"
                @edit="openMessageEditor"
                @revision="cycleRevision"
                @preview="openPreview"
                @media-loaded="scrollToLatest"
              />
            </template>
          </div>
          <button
            v-if="showScrollToLatest"
            class="scroll-to-latest"
            type="button"
            aria-label="回到最新消息"
            @click="scrollToLatest(true)"
          >
            <ArrowDown :size="15" :stroke-width="2.2" aria-hidden="true" />
            <span>最新</span>
          </button>
        </div>

        <p v-if="asrNotice" class="composer-notice asr-notice" role="status">{{ asrNotice }}</p>
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
              placeholder="有什么想聊的？"
              :disabled="sending"
              @keydown="handleComposerKeydown"
              @input="handleComposerInput"
              @focus="handleComposerFocus"
              @blur="scheduleHideModeSuggestions"
            ></textarea>
          </div>
          <button
            v-if="asrAvailable"
            class="composer__mic"
            :class="{ recording: asrState === 'recording' }"
            type="button"
            :disabled="asrState === 'uploading'"
            :aria-label="
              asrState === 'recording' ? '停止录音并识别' : asrState === 'uploading' ? '正在识别' : '语音输入'
            "
            :title="
              asrState === 'recording' ? '停止录音并识别' : asrState === 'uploading' ? '正在识别' : '语音输入'
            "
            @click="toggleVoiceInput"
          >
            <LoaderCircle
              v-if="asrState === 'uploading'"
              class="spinning"
              :size="18"
              :stroke-width="2"
              aria-hidden="true"
            />
            <Square v-else-if="asrState === 'recording'" :size="16" fill="currentColor" aria-hidden="true" />
            <Mic v-else :size="18" :stroke-width="2" aria-hidden="true" />
          </button>
          <button
            class="composer__send"
            :class="{ 'is-stopping': sending }"
            :type="sending ? 'button' : 'submit'"
            :disabled="!sending && !draft.trim()"
            :aria-label="sending ? '停止生成' : '发送消息'"
            :title="sending ? '停止生成' : '发送消息'"
            @click="handleSendButton"
          >
            <Square v-if="sending" :size="15" fill="currentColor" aria-hidden="true" />
            <Send v-else :size="18" :stroke-width="2" aria-hidden="true" />
          </button>
        </form>
      </section>

      <TasksView
        v-else-if="activeView === 'tasks'"
        :loading="imageStateLoading"
        :error="imageStateError"
        @preview="openPreview"
      />
      <GalleryView
        v-else-if="activeView === 'gallery'"
        :loading="imageStateLoading"
        :error="imageStateError"
      />
      <SettingsView v-else :session-id="api.sessionId" @logout="logout" />

      <BottomNav :active="activeView" @select="selectView" />

      <MessageEditDialog
        v-model="editDraft"
        :open="Boolean(editingMessageId)"
        @cancel="closeMessageEditor"
        @save="saveMessageEditor"
      />

      <button
        v-if="audioUnlockRequired"
        class="audio-unlock"
        type="button"
        @click="void unlockAudio()"
      >
        🔈 点击开启语音播放
      </button>

      <ImageViewer
        v-if="previewItems.length"
        v-model:index="previewIndex"
        :items="previewItems"
        @close="closePreview"
      />
    </section>
  </main>
</template>
