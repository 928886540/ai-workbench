<script setup lang="ts">
import { Check, CircleAlert, Copy, History, LoaderCircle, Pause, Pencil, Play, RotateCcw, Square, Volume2 } from "@lucide/vue";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { ChatMessage, MessageToolCall } from "../stores/messages";
import { activeVoiceId, voiceEnabled } from "../stores/voice";
import { renderExplicitImages, renderMarkdown } from "../utils/markdown";
import {
  getSpeechError,
  getSpeechStatus,
  speakMessage,
  stopSpeech,
  unlockAudio,
} from "../utils/speech";

const props = defineProps<{ message: ChatMessage }>();
const emit = defineEmits<{
  retry: [messageId: string];
  edit: [messageId: string, text: string];
  revision: [messageId: string];
  preview: [url: string];
  mediaLoaded: [];
}>();

const editing = ref(false);
const editDraft = ref("");
const editor = ref<HTMLTextAreaElement | null>(null);
const copied = ref(false);
let copiedTimer: number | null = null;

const displayedMessage = computed(() => {
  const revision = props.message.revisions[props.message.revisionIndex];
  return revision ? { ...props.message, ...revision } : props.message;
});
const viewingCurrentRevision = computed(
  () => props.message.revisionIndex === props.message.revisions.length,
);
const revisionLabel = computed(
  () => `${props.message.revisionIndex + 1} / ${props.message.revisions.length + 1}`,
);

const renderedBody = computed(() => {
  const message = displayedMessage.value;
  if (message.role !== "agent" || message.status === "error") return "";
  return `${renderExplicitImages(
    message.images,
    message.text,
  )}${renderMarkdown(message.text)}`;
});

const voiceClip = computed(() => displayedMessage.value.audio || displayedMessage.value.voice || null);
const isVoiceMessage = computed(() => voiceClip.value !== null);
const voiceAudio = ref<HTMLAudioElement | null>(null);
const voicePlaying = ref(false);
const voiceLoading = ref(false);
const voiceCurrentTime = ref(0);
const voiceDuration = ref(0);
let autoplayClipId = "";

const showToolbar = computed(
  () =>
    (displayedMessage.value.role === "agent" || displayedMessage.value.role === "user") &&
    ["done", "error", "cancelled"].includes(displayedMessage.value.status) &&
    displayedMessage.value.kind !== "image-result" &&
    !isVoiceMessage.value &&
    (Boolean(displayedMessage.value.text.trim()) ||
      displayedMessage.value.images.length > 0 ||
      displayedMessage.value.status !== "done"),
);

const canSpeak = computed(
  () =>
    voiceEnabled.value &&
    displayedMessage.value.role === "agent" &&
    displayedMessage.value.status === "done" &&
    displayedMessage.value.kind !== "image-result" &&
    !isVoiceMessage.value &&
    Boolean(displayedMessage.value.text.trim()),
);
const currentSpeechStatus = computed(() => getSpeechStatus(props.message.id));
const speechLabel = computed(() => {
  if (currentSpeechStatus.value === "loading") return "生成中…";
  if (currentSpeechStatus.value === "playing") return "停止";
  return "朗读";
});
const speechTitle = computed(() => getSpeechError(props.message.id) || speechLabel.value);

function elapsedLabel(): string {
  const elapsed = displayedMessage.value.meta.elapsedMs;
  if (elapsed === null) return "";
  return elapsed < 1000 ? `${elapsed}ms` : `${(elapsed / 1000).toFixed(1)}s`;
}

function formatTokenCount(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}k` : `${value}`;
}

function tokensLabel(): string {
  const { tokensIn, tokensOut } = displayedMessage.value.meta;
  if (tokensIn === null && tokensOut === null) return "";
  const parts: string[] = [];
  if (tokensIn !== null) parts.push(`↑${formatTokenCount(tokensIn)}`);
  if (tokensOut !== null) parts.push(`↓${formatTokenCount(tokensOut)}`);
  return parts.join(" ");
}

function voiceSizeLabel(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function voiceTimeLabel(seconds: number): string {
  const safe = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
  const minutes = Math.floor(safe / 60);
  return `${String(minutes).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
}

function syncVoiceDuration(): void {
  const audio = voiceAudio.value;
  voiceDuration.value = audio && Number.isFinite(audio.duration) ? audio.duration : 0;
  emit("mediaLoaded");
}

function syncVoiceTime(): void {
  voiceCurrentTime.value = voiceAudio.value?.currentTime || 0;
}

function handleExclusiveVoicePlay(event: Event): void {
  const activeId = (event as CustomEvent<string>).detail;
  if (activeId === props.message.id) return;
  voiceAudio.value?.pause();
}

async function playVoiceBubble(autoplay = false): Promise<void> {
  const audio = voiceAudio.value;
  if (!audio) return;
  stopSpeech();
  window.dispatchEvent(new CustomEvent("leon:voice-play", { detail: props.message.id }));
  voiceLoading.value = true;
  try {
    await audio.play();
    voicePlaying.value = true;
  } catch (error) {
    voicePlaying.value = false;
    if (!autoplay) {
      // A direct tap normally succeeds; keep the control reusable when the
      // browser still rejects playback because the clip has not loaded yet.
      audio.load();
    }
  } finally {
    voiceLoading.value = false;
  }
}

function toggleVoiceBubble(): void {
  const audio = voiceAudio.value;
  if (!audio) return;
  if (!audio.paused) {
    audio.pause();
    return;
  }
  void playVoiceBubble();
}

function seekVoice(event: Event): void {
  const audio = voiceAudio.value;
  if (!audio) return;
  const value = Number((event.target as HTMLInputElement).value);
  if (Number.isFinite(value)) {
    audio.currentTime = value;
    voiceCurrentTime.value = value;
  }
}

function tryAutoplayVoice(): void {
  const clipId = voiceClip.value?.clipId || "";
  if (!clipId || autoplayClipId === clipId) return;
  autoplayClipId = clipId;
  void playVoiceBubble(true);
}

function toolLabel(tool: MessageToolCall): string {
  const labels: Record<string, string> = {
    generate_images: "Leon 生图",
    list_image_modes: "读取生图模式",
    check_image_environment: "检查生图环境",
    get_image_tasks: "查询生图任务",
    get_recent_images: "读取会话图库",
    get_latest_images: "读取最近图片",
    cancel_image_task: "取消生图任务",
    speak_text: "生成语音",
  };
  return labels[tool.name] || "调用工具";
}

function toolStatusLabel(tool: MessageToolCall): string {
  if (tool.status === "running") return "执行中";
  if (tool.status === "cancelled") return "已停止";
  if (tool.status === "error") return "失败";
  if (tool.name !== "generate_images") return "已完成";
  const jobs = Array.isArray(tool.output.jobs) ? tool.output.jobs : [];
  const rawBatchCount = Number(tool.input.batch_count || 1);
  const workflowIds = Array.isArray(tool.output.workflow_ids)
    ? tool.output.workflow_ids
    : Array.isArray(tool.input.workflow_ids)
      ? tool.input.workflow_ids
      : [];
  const count = jobs.length || Math.max(1, workflowIds.length || 1) * Math.max(1, rawBatchCount || 1);
  return `${count} 张已提交`;
}

function toolSummary(tool: MessageToolCall): string {
  const sourceText = tool.input.source_text;
  if (typeof sourceText === "string" && sourceText.trim()) return sourceText.trim();
  const error = tool.output.error;
  if (tool.status === "error" && typeof error === "string") return error;
  return "";
}

async function copyText(): Promise<void> {
  const text = displayedMessage.value.text;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }
  copied.value = true;
  if (copiedTimer !== null) window.clearTimeout(copiedTimer);
  copiedTimer = window.setTimeout(() => {
    copied.value = false;
    copiedTimer = null;
  }, 1400);
}

function startEditing(): void {
  if (
    !viewingCurrentRevision.value ||
    isVoiceMessage.value ||
    displayedMessage.value.kind === "image-result"
  ) return;
  editDraft.value = displayedMessage.value.text;
  editing.value = true;
  void nextTick(() => {
    editor.value?.focus();
    editor.value?.setSelectionRange(editDraft.value.length, editDraft.value.length);
  });
}

function cancelEditing(): void {
  editing.value = false;
  editDraft.value = "";
}

function saveEditing(): void {
  stopSpeech(props.message.id);
  emit("edit", props.message.id, editDraft.value);
  editing.value = false;
}

function toggleSpeech(): void {
  if (currentSpeechStatus.value !== "idle") {
    stopSpeech(props.message.id);
    return;
  }
  // This runs synchronously inside the click gesture; the TTS fetch happens
  // afterwards and therefore cannot be trusted to unlock iOS audio by itself.
  void unlockAudio();
  void speakMessage(props.message.id, displayedMessage.value.text, activeVoiceId());
}

function handleContentClick(event: MouseEvent): void {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const link = target.closest("a.markdown-image-link") as HTMLAnchorElement | null;
  if (!link) return;
  event.preventDefault();
  emit("preview", link.href);
}

onBeforeUnmount(() => {
  if (copiedTimer !== null) window.clearTimeout(copiedTimer);
  stopSpeech(props.message.id);
  window.removeEventListener("leon:voice-play", handleExclusiveVoicePlay as EventListener);
  voiceAudio.value?.pause();
});

onMounted(() => {
  window.addEventListener("leon:voice-play", handleExclusiveVoicePlay as EventListener);
  void nextTick(tryAutoplayVoice);
});

watch(
  () => voiceClip.value?.clipId,
  () => {
    voicePlaying.value = false;
    voiceCurrentTime.value = 0;
    voiceDuration.value = 0;
    void nextTick(tryAutoplayVoice);
  },
);
</script>

<template>
  <article class="message-row" :data-role="message.role" :data-kind="message.kind">
    <div class="message-stack">
      <template v-if="editing && !isVoiceMessage">
        <textarea
          ref="editor"
          v-model="editDraft"
          class="message-editor"
          rows="4"
          @keydown.escape.prevent="cancelEditing"
          @keydown.ctrl.enter.prevent="saveEditing"
          @keydown.meta.enter.prevent="saveEditing"
        ></textarea>
        <div class="message-edit-actions">
          <button type="button" @click="cancelEditing">取消</button>
          <button class="primary-small" type="button" @click="saveEditing">保存</button>
        </div>
      </template>
      <div
        v-else
        class="message-bubble"
        :data-status="displayedMessage.status"
        @click="handleContentClick"
        @load.capture="emit('mediaLoaded')"
      >
        <div v-if="displayedMessage.tools.length" class="message-tools" aria-label="工具执行状态">
          <div
            v-for="tool in displayedMessage.tools"
            :key="tool.id"
            class="message-tool"
            :data-status="tool.status"
          >
            <span class="message-tool__icon" aria-hidden="true">
              <LoaderCircle v-if="tool.status === 'running'" class="spinning" :size="17" />
              <CircleAlert v-else-if="tool.status === 'error'" :size="17" />
              <Square v-else-if="tool.status === 'cancelled'" :size="15" fill="currentColor" />
              <Check v-else :size="17" :stroke-width="2.2" />
            </span>
            <span class="message-tool__body">
              <strong>{{ toolLabel(tool) }}</strong>
              <small v-if="toolSummary(tool)">{{ toolSummary(tool) }}</small>
            </span>
            <span class="message-tool__status">{{ toolStatusLabel(tool) }}</span>
          </div>
        </div>
        <div v-if="displayedMessage.status === 'error'" class="message-error" role="alert">
          <p class="message-error__summary">请求失败</p>
          <p class="message-error__hint">可以重试，或展开查看原始错误。</p>
          <details class="message-error__details">
            <summary>查看错误详情</summary>
            <pre class="message-error__raw">{{ displayedMessage.text }}</pre>
          </details>
        </div>
        <div v-else-if="displayedMessage.status === 'cancelled'" class="message-cancelled" role="status">
          <Square :size="14" fill="currentColor" aria-hidden="true" />
          <span>已停止</span>
        </div>
        <div v-else-if="voiceClip" class="voice-bubble" data-kind="voice">
          <div class="voice-bubble__meta">
            <span class="voice-bubble__name">
              <Volume2 :size="15" :stroke-width="2" aria-hidden="true" />
              {{ voiceClip.voiceName || "语音" }}
            </span>
            <span v-if="voiceSizeLabel(voiceClip.bytes)" class="voice-bubble__size">
              {{ voiceSizeLabel(voiceClip.bytes) }}
            </span>
          </div>
          <div class="voice-player" :data-playing="voicePlaying">
            <button
              class="voice-player__toggle"
              type="button"
              :aria-label="voicePlaying ? '暂停语音' : '播放语音'"
              :title="voicePlaying ? '暂停' : '播放'"
              @click="toggleVoiceBubble"
            >
              <LoaderCircle v-if="voiceLoading" class="spinning" :size="17" aria-hidden="true" />
              <Pause v-else-if="voicePlaying" :size="16" fill="currentColor" aria-hidden="true" />
              <Play v-else :size="16" fill="currentColor" aria-hidden="true" />
            </button>
            <div class="voice-player__track">
              <input
                type="range"
                min="0"
                :max="Math.max(voiceDuration, 0)"
                step="0.1"
                :value="voiceCurrentTime"
                aria-label="语音播放进度"
                @input="seekVoice"
              />
              <span>
                {{ voiceTimeLabel(voiceCurrentTime) }} / {{ voiceTimeLabel(voiceDuration) }}
              </span>
            </div>
          </div>
          <audio
            ref="voiceAudio"
            class="voice-bubble__audio"
            preload="metadata"
            playsinline
            :src="voiceClip.url"
            @loadedmetadata="syncVoiceDuration"
            @canplay="tryAutoplayVoice"
            @timeupdate="syncVoiceTime"
            @play="voicePlaying = true"
            @pause="voicePlaying = false"
            @ended="voicePlaying = false; voiceCurrentTime = 0"
          ></audio>
          <p v-if="voiceClip.text || displayedMessage.text" class="voice-bubble__text">
            {{ voiceClip.text || displayedMessage.text }}
          </p>
        </div>
        <div
          v-else-if="displayedMessage.role === 'agent' && renderedBody"
          class="message-content"
          v-html="renderedBody"
        ></div>
        <p v-else-if="displayedMessage.text" class="message-text">{{ displayedMessage.text }}</p>
        <div
          v-if="displayedMessage.status === 'pending' && !displayedMessage.tools.length"
          class="thinking"
          role="status"
          aria-label="正在思考"
        >
          <i></i><i></i><i></i>
        </div>
      </div>

      <div v-if="message.role === 'agent' && message.revisions.length" class="message-revision">
        <button
          type="button"
          :aria-label="`切换回答版本，当前 ${revisionLabel}`"
          :title="`切换回答版本，当前 ${revisionLabel}`"
          @click="emit('revision', message.id)"
        >
          <History :size="14" :stroke-width="2" aria-hidden="true" />
          <span>{{ revisionLabel }}</span>
        </button>
      </div>
      <div v-if="!isVoiceMessage && showToolbar && !editing" class="message-toolbar">
        <button
          v-if="canSpeak"
          class="message-speak"
          type="button"
          :data-state="currentSpeechStatus"
          :title="speechTitle"
          :aria-label="speechLabel"
          @click="toggleSpeech"
        >
          <span v-if="currentSpeechStatus === 'loading'" class="speech-spinner" aria-hidden="true"></span>
          <span v-else-if="currentSpeechStatus === 'playing'" class="speech-wave" aria-hidden="true">
            <i></i><i></i><i></i><i></i>
          </span>
          <Volume2 v-else class="toolbar-icon" :size="16" :stroke-width="2" aria-hidden="true" />
        </button>
        <button
          v-if="displayedMessage.text"
          type="button"
          :aria-label="copied ? '已复制' : '复制'"
          :title="copied ? '已复制' : '复制'"
          @click="void copyText()"
        >
          <Check v-if="copied" class="toolbar-icon" :size="16" :stroke-width="2.2" aria-hidden="true" />
          <Copy v-else class="toolbar-icon" :size="16" :stroke-width="2" aria-hidden="true" />
        </button>
        <button type="button" aria-label="重试" title="重试" @click="emit('retry', message.id)">
          <RotateCcw class="toolbar-icon" :size="16" :stroke-width="2" aria-hidden="true" />
        </button>
        <button
          v-if="displayedMessage.text && viewingCurrentRevision"
          type="button"
          aria-label="编辑"
          title="编辑"
          @click="startEditing"
        >
          <Pencil class="toolbar-icon" :size="16" :stroke-width="2" aria-hidden="true" />
        </button>
        <span v-if="elapsedLabel() || tokensLabel()" class="message-meta">
          <span v-if="elapsedLabel()">{{ elapsedLabel() }}</span>
          <span v-if="tokensLabel()">{{ tokensLabel() }}</span>
        </span>
      </div>
    </div>
  </article>
</template>
