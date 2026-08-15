<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import AppStatus from "../components/AppStatus.vue";
import BottomNav, { type WorkbenchView } from "../components/BottomNav.vue";
import { ApiError, api, type LeonEvent } from "../api/client";
import GalleryView from "./GalleryView.vue";
import SettingsView from "./SettingsView.vue";
import TasksView from "./TasksView.vue";
import {
  appendMessage,
  clearMessages,
  findMessage,
  latestMessage,
  makeMessage,
  messages,
  type ChatMessage,
} from "../stores/messages";
import {
  clearImageState,
  completeImageTask,
  hydrateImageState,
  upsertImageTask,
} from "../stores/images";

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
const messagesPanel = ref<HTMLElement | null>(null);
const pendingAssistantId = ref<string | null>(null);
let eventSource: EventSource | null = null;
let reconnectTimer: number | null = null;
let lastAgentErrorAt = 0;

function asString(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function setConnection(label: string, tone: "neutral" | "ok" | "error"): void {
  connectionLabel.value = label;
  connectionTone.value = tone;
}

function scrollToLatest(): void {
  void nextTick(() => {
    const panel = messagesPanel.value;
    if (panel) panel.scrollTop = panel.scrollHeight;
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
  activeView.value = view;
  if (view === "tasks" || view === "gallery") void loadImageState();
  if (view === "chat") scrollToLatest();
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
  scrollToLatest();
}

function pendingAssistant(): ChatMessage | null {
  return findMessage(pendingAssistantId.value);
}

function finishAssistant(content: string, status: "done" | "error" = "done"): void {
  const current = pendingAssistant();
  if (current) {
    current.text = content;
    current.status = status;
    current.meta.finishedAt = Date.now();
    current.meta.elapsedMs = current.meta.startedAt
      ? current.meta.finishedAt - current.meta.startedAt
      : null;
  } else if (content) {
    appendMessage(makeMessage("agent", content, status));
  }
  pendingAssistantId.value = null;
  taskStatus.value = "";
  scrollToLatest();
}

function handleEvent(event: LeonEvent): void {
  const data = event.data || {};
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
    case "assistant.completed":
      finishAssistant(asString(data.content));
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
  connectEvents();
  void loadImageState();
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
  api.logout();
  authenticated.value = false;
  clearMessages();
  clearImageState();
  imageStateLoading.value = false;
  imageStateLoaded.value = false;
  imageStateError.value = "";
  activeView.value = "chat";
  pendingAssistantId.value = null;
  setConnection("已退出", "neutral");
}

async function sendMessage(): Promise<void> {
  const content = draft.value.trim();
  if (!content || sending.value || !api.sessionId) return;
  const previousAgentId = latestMessage("agent")?.id || null;
  // A POST fallback may retain its id to absorb a late SSE completion. Once a
  // new turn starts, that id belongs to the previous turn and must not be
  // reused by the next assistant.started event.
  pendingAssistantId.value = null;
  appendMessage(makeMessage("user", content));
  draft.value = "";
  sending.value = true;
  taskStatus.value = "正在发送…";
  scrollToLatest();
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
    } else {
      const fallback = appendMessage(makeMessage("agent", response.answer, response.ok ? "done" : "error"));
      pendingAssistantId.value = fallback.id;
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
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    void sendMessage();
  }
}

onMounted(() => void bootstrap());
onBeforeUnmount(closeEvents);
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
          <AppStatus :label="connectionLabel" :tone="connectionTone" />
          <button class="ghost-button" type="button" @click="logout">退出</button>
        </div>
      </header>

      <section v-if="activeView === 'chat'" class="chat-view" aria-label="聊天">
        <div ref="messagesPanel" class="messages-panel" aria-live="polite">
          <div v-if="!messages.length" class="empty-state">
            <strong>开始一段对话</strong>
            <span>普通聊天和生图请求都会沿用现有 Gateway 协议。</span>
          </div>
          <article
            v-for="message in messages"
            :key="message.id"
            class="message-row"
            :data-role="message.role"
          >
            <div class="message-bubble" :data-status="message.status">
              <p v-if="message.text" class="message-text">{{ message.text }}</p>
              <p v-if="message.status === 'pending'" class="thinking">思考中…</p>
              <img
                v-for="image in message.images"
                :key="image"
                class="message-image"
                :src="image"
                alt="生成图片"
              />
            </div>
          </article>
        </div>

        <p v-if="taskStatus" class="task-status">{{ taskStatus }}</p>
        <form class="composer" @submit.prevent="sendMessage">
          <textarea
            v-model="draft"
            rows="1"
            maxlength="8000"
            placeholder="输入消息，Enter 发送，Shift+Enter 换行"
            :disabled="sending"
            @keydown="handleComposerKeydown"
          ></textarea>
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
    </section>
  </main>
</template>
