<script setup lang="ts">
import { LoaderCircle, MessageSquarePlus, Pin, PinOff, X } from "@lucide/vue";
import { onMounted, ref } from "vue";
import type { SessionSummary } from "../api/client";

defineProps<{
  sessions: SessionSummary[];
  activeSessionId: string;
  loading: boolean;
  error: string;
  switchingSessionId: string;
  pinningSessionId: string;
  creating: boolean;
}>();

const emit = defineEmits<{
  close: [];
  create: [];
  select: [sessionId: string];
  "toggle-pin": [sessionId: string, pinned: boolean];
}>();

const panel = ref<HTMLElement | null>(null);

onMounted(() => panel.value?.focus());

function selectSession(sessionId: string): void {
  emit("select", sessionId);
}

function togglePin(sessionId: string, pinned: boolean): void {
  emit("toggle-pin", sessionId, pinned);
}

function timeLabel(timestamp: number): string {
  if (!timestamp) return "";
  const value = new Date(timestamp);
  const today = new Date();
  if (value.toDateString() === today.toDateString()) {
    return value.toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }
  return value.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}
</script>

<template>
  <aside
    id="session-history-panel"
    ref="panel"
    class="session-history-panel"
    tabindex="-1"
    role="dialog"
    aria-modal="false"
    aria-label="历史会话"
    @keydown.escape="emit('close')"
  >
    <header class="session-history-panel__header">
      <div>
        <h2>历史会话</h2>
        <p>{{ sessions.length }} 个会话</p>
      </div>
      <div class="session-history-panel__actions">
        <button
          class="icon-button icon-button--subtle"
          type="button"
          :disabled="creating || Boolean(switchingSessionId)"
          aria-label="新建会话"
          title="新建会话"
          @click="emit('create')"
        >
          <LoaderCircle v-if="creating" class="spinning" :size="17" aria-hidden="true" />
          <MessageSquarePlus v-else :size="17" aria-hidden="true" />
        </button>
        <button
          class="icon-button icon-button--subtle"
          type="button"
          aria-label="关闭历史会话"
          title="关闭"
          @click="emit('close')"
        >
          <X :size="17" aria-hidden="true" />
        </button>
      </div>
    </header>

    <p v-if="error" class="session-history-panel__error">{{ error }}</p>
    <div v-if="loading && !sessions.length" class="session-history-panel__empty">
      <LoaderCircle class="spinning" :size="18" aria-hidden="true" />
      <span>正在加载…</span>
    </div>
    <p v-else-if="!sessions.length" class="session-history-panel__empty">暂无会话</p>
    <ol v-else class="session-history-list">
      <li
        v-for="session in sessions"
        :key="session.id"
        class="session-history-entry"
        :class="{ 'is-active': session.id === activeSessionId }"
      >
        <button
          class="session-history-entry__select"
          type="button"
          :disabled="Boolean(switchingSessionId)"
          :aria-current="session.id === activeSessionId ? 'true' : undefined"
          @click="selectSession(session.id)"
        >
          <span class="session-history-entry__title">
            <strong>{{ session.title || "新会话" }}</strong>
            <span v-if="session.id === activeSessionId">当前</span>
          </span>
          <span class="session-history-entry__meta">
            <span>{{ timeLabel(session.updated_at) }}</span>
            <span>{{ session.message_count }} 条消息</span>
          </span>
        </button>
        <LoaderCircle
          v-if="switchingSessionId === session.id"
          class="session-history-entry__spinner spinning"
          :size="17"
          aria-label="正在切换"
        />
        <button
          v-else
          class="session-history-entry__pin"
          type="button"
          :class="{ 'is-pinned': session.pinned }"
          :disabled="Boolean(pinningSessionId)"
          :aria-label="session.pinned ? '取消置顶' : '置顶会话'"
          :title="session.pinned ? '取消置顶' : '置顶会话'"
          @click="togglePin(session.id, !session.pinned)"
        >
          <LoaderCircle
            v-if="pinningSessionId === session.id"
            class="spinning"
            :size="16"
            aria-hidden="true"
          />
          <PinOff v-else-if="session.pinned" :size="16" aria-hidden="true" />
          <Pin v-else :size="16" aria-hidden="true" />
        </button>
      </li>
    </ol>
  </aside>
</template>
