<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from "vue";
import type { ChatMessage } from "../stores/messages";
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
  preview: [url: string];
  mediaLoaded: [];
}>();

const editing = ref(false);
const editDraft = ref("");
const editor = ref<HTMLTextAreaElement | null>(null);
const copied = ref(false);
let copiedTimer: number | null = null;

const renderedBody = computed(() => {
  if (props.message.role !== "agent" || props.message.status === "error") return "";
  return `${renderMarkdown(props.message.text)}${renderExplicitImages(
    props.message.images,
    props.message.text,
  )}`;
});

const voiceClip = computed(() => props.message.audio || props.message.voice || null);
const isVoiceMessage = computed(() => voiceClip.value !== null);

const showToolbar = computed(
  () =>
    props.message.role === "agent" &&
    props.message.status === "done" &&
    !isVoiceMessage.value &&
    (Boolean(props.message.text.trim()) || props.message.images.length > 0),
);

const canSpeak = computed(
  () =>
    voiceEnabled.value &&
    props.message.role === "agent" &&
    props.message.status === "done" &&
    !isVoiceMessage.value &&
    Boolean(props.message.text.trim()),
);
const currentSpeechStatus = computed(() => getSpeechStatus(props.message.id));
const speechLabel = computed(() => {
  if (currentSpeechStatus.value === "loading") return "生成中…";
  if (currentSpeechStatus.value === "playing") return "停止";
  return "朗读";
});
const speechTitle = computed(() => getSpeechError(props.message.id) || speechLabel.value);

function elapsedLabel(): string {
  const elapsed = props.message.meta.elapsedMs;
  if (elapsed === null) return "";
  return elapsed < 1000 ? `${elapsed}ms` : `${(elapsed / 1000).toFixed(1)}s`;
}

function voiceSizeLabel(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function copyText(): Promise<void> {
  const text = props.message.text;
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
  if (isVoiceMessage.value) return;
  editDraft.value = props.message.text;
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
  void speakMessage(props.message.id, props.message.text, activeVoiceId());
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
});
</script>

<template>
  <article class="message-row" :data-role="message.role">
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
        :data-status="message.status"
        @click="handleContentClick"
        @load.capture="emit('mediaLoaded')"
      >
        <p v-if="message.status === 'error'" class="message-text">{{ message.text }}</p>
        <div v-else-if="voiceClip" class="voice-bubble" data-kind="voice">
          <div class="voice-bubble__meta">
            <span class="voice-bubble__name">🔊 {{ voiceClip.voiceName || "语音" }}</span>
            <span v-if="voiceSizeLabel(voiceClip.bytes)" class="voice-bubble__size">
              {{ voiceSizeLabel(voiceClip.bytes) }}
            </span>
          </div>
          <audio
            class="voice-bubble__audio"
            controls
            preload="metadata"
            playsinline
            :src="voiceClip.url"
            @loadedmetadata="emit('mediaLoaded')"
          ></audio>
          <p v-if="voiceClip.text || message.text" class="voice-bubble__text">
            {{ voiceClip.text || message.text }}
          </p>
        </div>
        <div
          v-else-if="message.role === 'agent' && renderedBody"
          class="message-content"
          v-html="renderedBody"
        ></div>
        <p v-else-if="message.text" class="message-text">{{ message.text }}</p>
        <p v-if="message.status === 'pending'" class="thinking">思考中…</p>
      </div>

      <div
        v-if="!isVoiceMessage && message.role === 'agent' && message.status === 'error'"
        class="message-toolbar"
      >
        <button type="button" @click="emit('retry', message.id)">重试</button>
      </div>
      <div v-else-if="!isVoiceMessage && showToolbar && !editing" class="message-toolbar">
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
          <span>{{ speechLabel }}</span>
        </button>
        <button v-if="message.text" type="button" @click="void copyText()">
          {{ copied ? "✓ 已复制" : "复制" }}
        </button>
        <button type="button" @click="emit('retry', message.id)">重试</button>
        <button v-if="message.text" type="button" @click="startEditing">编辑</button>
        <span v-if="elapsedLabel()">{{ elapsedLabel() }}</span>
      </div>
    </div>
  </article>
</template>
