<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
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
} from "../stores/voice";

const loading = ref(false);
const search = ref("");
const status = ref("");
const statusTone = ref<"neutral" | "error">("neutral");
let demoAudio: HTMLAudioElement | null = null;

const visibleVoices = computed(() => {
  const query = search.value.trim().toLocaleLowerCase();
  const matches = voices.value.filter(
    (voice) =>
      !query ||
      voice.name.toLocaleLowerCase().includes(query) ||
      voice.model.toLocaleLowerCase().includes(query),
  );
  const favorites = matches.filter((voice) => isFavoriteVoice(voice.id));
  const rest = matches.filter((voice) => !isFavoriteVoice(voice.id));
  if (query) return [...favorites, ...rest].slice(0, 60);
  if (favorites.length) return favorites;
  return rest.slice(0, 20);
});

const active = computed(() => activeVoice());

async function loadCatalog(refresh = false): Promise<void> {
  if (loading.value) return;
  if (voiceCatalogLoaded.value && !refresh) {
    statusTone.value = "neutral";
    status.value = voiceEnabled.value
      ? `共 ${voices.value.length} 个音色，搜索名称或模型；★ 可收藏`
      : "未配置 VOLINK_API_KEY，语音功能已关闭";
    return;
  }
  loading.value = true;
  status.value = refresh ? "正在刷新音色目录…" : "正在加载音色目录…";
  statusTone.value = "neutral";
  try {
    await loadVoiceCatalog(refresh);
    status.value = voiceEnabled.value
      ? `共 ${voices.value.length} 个音色，搜索名称或模型；★ 可收藏`
      : "未配置 VOLINK_API_KEY，语音功能已关闭";
  } catch (error) {
    statusTone.value = "error";
    status.value =
      error instanceof ApiError && error.status === 401
        ? "Token 已失效，请重新登录"
        : `加载失败：${error instanceof Error ? error.message : "未知错误"}`;
  } finally {
    loading.value = false;
  }
}

function chooseVoice(id: string): void {
  selectVoice(id);
  statusTone.value = "neutral";
  status.value = "已选择音色，下次朗读立即生效";
}

function playDemo(url: string): void {
  if (!url) return;
  demoAudio?.pause();
  demoAudio = new Audio(url);
  void demoAudio.play().catch(() => {
    statusTone.value = "error";
    status.value = "试听被浏览器拦截，请先点击页面后重试";
  });
}

onMounted(() => void loadCatalog());
onBeforeUnmount(() => {
  demoAudio?.pause();
  demoAudio = null;
});
</script>

<template>
  <article class="settings-card voice-settings">
    <header class="voice-settings__header">
      <div>
        <h3>语音</h3>
        <p class="settings-subtitle">
          当前：{{ active?.name || "跟随服务端默认" }}
          <span v-if="active?.model"> · {{ active.model }}</span>
        </p>
      </div>
      <button class="ghost-button" type="button" :disabled="loading" @click="void loadCatalog(true)">
        {{ loading ? "加载中…" : "刷新" }}
      </button>
    </header>

    <template v-if="voiceEnabled">
      <label class="settings-toggle">
        <span>
          <strong>自动朗读每条回复</strong>
          <small>关闭后仍可在气泡上手动朗读</small>
        </span>
        <input
          type="checkbox"
          :checked="autoplayAll"
          @change="setAutoplayAll(($event.target as HTMLInputElement).checked)"
        />
      </label>
      <div class="voice-search-row">
        <input v-model="search" type="search" placeholder="搜索音色名 / 模型" aria-label="搜索音色" />
        <span>{{ voices.length }} 个</span>
      </div>
      <div v-if="!visibleVoices.length" class="voice-empty">没有匹配的音色</div>
      <div v-else class="voice-list" role="listbox" aria-label="音色">
        <div
          v-for="voice in visibleVoices"
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
            @click="toggleFavoriteVoice(voice.id)"
          >
            {{ isFavoriteVoice(voice.id) ? "★" : "☆" }}
          </button>
          <button v-if="voice.demo" class="voice-option__demo" type="button" @click="playDemo(voice.demo)">
            ▶
          </button>
        </div>
      </div>
    </template>
    <p v-else class="voice-disabled">{{ loading ? "正在检查语音配置…" : "未配置 VOLINK_API_KEY，语音功能已关闭" }}</p>
    <p class="settings-status" :data-tone="statusTone" role="status">{{ status }}</p>
  </article>
</template>
