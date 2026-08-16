<script setup lang="ts">
import { ChevronDown, ChevronUp, LoaderCircle, Pause, Play, RefreshCw, Star } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ApiError } from "../api/client";
import {
  activeVoice,
  autoplayAll,
  defaultVoiceId,
  isFavoriteVoice,
  loadVoiceCatalog,
  selectVoice,
  setAutoplayAll,
  voiceCatalogLoaded,
  toggleFavoriteVoice,
  voiceEnabled,
  voices,
  type VoiceOption,
} from "../stores/voice";

const PAGE_SIZE = 20;

const loading = ref(false);
const search = ref("");
const status = ref("");
const statusTone = ref<"neutral" | "error">("neutral");
const catalogOpen = ref(false);
const voiceTab = ref<"all" | "favorites">("all");
const page = ref(1);
const demoVoiceId = ref("");
const demoState = ref<"idle" | "loading" | "playing">("idle");
let demoAudio: HTMLAudioElement | null = null;

const filteredVoices = computed(() => {
  const query = search.value.trim().toLocaleLowerCase();
  const matches = voices.value.filter(
    (voice) =>
      !query ||
      voice.name.toLocaleLowerCase().includes(query) ||
      voice.model.toLocaleLowerCase().includes(query),
  );
  if (voiceTab.value === "favorites") return matches.filter((voice) => isFavoriteVoice(voice.id));
  return matches;
});

const pageCount = computed(() => Math.max(1, Math.ceil(filteredVoices.value.length / PAGE_SIZE)));

const pagedVoices = computed(() =>
  filteredVoices.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE),
);

const active = computed(() => activeVoice());

watch([search, voiceTab], () => {
  page.value = 1;
});

function stopDemo(): void {
  demoAudio?.pause();
  demoAudio = null;
  demoVoiceId.value = "";
  demoState.value = "idle";
}

async function loadCatalog(refresh = false): Promise<void> {
  if (loading.value) return;
  if (voiceCatalogLoaded.value && !refresh) return;
  loading.value = true;
  status.value = refresh ? "正在刷新…" : "正在加载…";
  statusTone.value = "neutral";
  try {
    await loadVoiceCatalog(refresh);
    status.value = "";
  } catch (error) {
    statusTone.value = "error";
    status.value =
      error instanceof ApiError && error.status === 401
        ? "登录已失效，请重新登录"
        : `加载失败：${error instanceof Error ? error.message : "未知错误"}`;
  } finally {
    loading.value = false;
  }
}

function chooseVoice(id: string): void {
  selectVoice(id);
}

function playDemo(voice: VoiceOption): void {
  if (!voice.demo) return;
  if (demoVoiceId.value === voice.id && demoState.value === "playing") {
    stopDemo();
    return;
  }

  stopDemo();
  const audio = new Audio(voice.demo);
  demoAudio = audio;
  demoVoiceId.value = voice.id;
  demoState.value = "loading";
  audio.addEventListener("ended", () => {
    if (demoAudio === audio) stopDemo();
  });
  void audio.play().then(() => {
    if (demoAudio !== audio) return;
    demoState.value = "playing";
  }).catch(() => {
    if (demoAudio !== audio) return;
    stopDemo();
    statusTone.value = "error";
    status.value = "试听被浏览器拦截，请点击页面后重试";
  });
}

onMounted(() => void loadCatalog());
onBeforeUnmount(() => {
  stopDemo();
});
</script>

<template>
  <article class="settings-card voice-settings">
    <header class="voice-settings__header">
      <div>
        <h3>语音</h3>
        <p class="settings-subtitle">
          当前：{{ active?.name || "跟随服务端默认" }}
        </p>
      </div>
    </header>

    <template v-if="voiceEnabled">
      <label class="settings-toggle">
        <span class="settings-toggle__copy">
          <strong>自动朗读每条回复</strong>
        </span>
        <input
          type="checkbox"
          :checked="autoplayAll"
          @change="setAutoplayAll(($event.target as HTMLInputElement).checked)"
        />
        <span class="settings-switch" aria-hidden="true"></span>
      </label>
      <button
        class="voice-catalog-toggle"
        type="button"
        :aria-expanded="catalogOpen"
        aria-controls="voice-catalog-panel"
        @click="catalogOpen = !catalogOpen"
      >
        <span><strong>选择音色</strong><small>{{ voices.length }} 个</small></span>
        <ChevronUp v-if="catalogOpen" :size="18" aria-hidden="true" />
        <ChevronDown v-else :size="18" aria-hidden="true" />
      </button>
      <div v-if="catalogOpen" id="voice-catalog-panel" class="voice-catalog-panel">
        <div class="voice-tabs" role="tablist" aria-label="音色分类">
          <button
            type="button"
            role="tab"
            :aria-selected="voiceTab === 'all'"
            :class="{ active: voiceTab === 'all' }"
            @click="voiceTab = 'all'"
          >
            全部
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="voiceTab === 'favorites'"
            :class="{ active: voiceTab === 'favorites' }"
            @click="voiceTab = 'favorites'"
          >
            收藏
          </button>
        </div>
        <div class="voice-search-row">
          <input v-model="search" type="search" placeholder="搜索音色" aria-label="搜索音色" />
          <button
            class="icon-button icon-button--subtle"
            type="button"
            :disabled="loading"
            aria-label="刷新音色"
            title="刷新音色"
            @click="void loadCatalog(true)"
          >
            <RefreshCw :class="{ spinning: loading }" :size="15" :stroke-width="2" aria-hidden="true" />
          </button>
        </div>
        <div v-if="!filteredVoices.length" class="voice-empty">
          {{ voiceTab === "favorites" ? "还没有收藏的音色" : "没有匹配的音色" }}
        </div>
        <div v-else class="voice-list" role="listbox" aria-label="音色">
          <div
            v-for="voice in pagedVoices"
            :key="voice.id"
            class="voice-option"
            :class="{ selected: voice.id === (active?.id || defaultVoiceId) }"
            role="option"
            :aria-selected="voice.id === (active?.id || defaultVoiceId)"
          >
            <button class="voice-option__main" type="button" @click="chooseVoice(voice.id)">
              <strong>{{ voice.name }}</strong>
              <small>{{ voice.model || "默认模型" }}</small>
            </button>
            <button
              class="voice-option__star"
              type="button"
              :aria-label="isFavoriteVoice(voice.id) ? '取消收藏' : '收藏音色'"
              :title="isFavoriteVoice(voice.id) ? '取消收藏' : '收藏音色'"
              @click="toggleFavoriteVoice(voice.id)"
            >
              <Star :size="17" :fill="isFavoriteVoice(voice.id) ? 'currentColor' : 'none'" aria-hidden="true" />
            </button>
            <button
              v-if="voice.demo"
              class="voice-option__demo"
              type="button"
              :data-state="demoVoiceId === voice.id ? demoState : 'idle'"
              :aria-label="demoVoiceId === voice.id && demoState === 'playing' ? '停止试听' : `试听 ${voice.name}`"
              :title="demoVoiceId === voice.id && demoState === 'playing' ? '停止试听' : '试听'"
              @click="playDemo(voice)"
            >
              <LoaderCircle v-if="demoVoiceId === voice.id && demoState === 'loading'" class="spinning" :size="17" aria-hidden="true" />
              <Pause v-else-if="demoVoiceId === voice.id && demoState === 'playing'" :size="17" fill="currentColor" aria-hidden="true" />
              <Play v-else :size="17" fill="currentColor" aria-hidden="true" />
            </button>
          </div>
        </div>
        <div v-if="pageCount > 1" class="voice-pager">
          <button type="button" :disabled="page <= 1" aria-label="上一页" @click="page -= 1">‹</button>
          <span>{{ page }} / {{ pageCount }}</span>
          <button type="button" :disabled="page >= pageCount" aria-label="下一页" @click="page += 1">›</button>
        </div>
      </div>
    </template>
    <p v-else class="voice-disabled">{{ loading ? "正在检查语音配置…" : "语音服务未配置" }}</p>
    <p v-if="status" class="settings-status" :data-tone="statusTone" role="status">{{ status }}</p>
  </article>
</template>
