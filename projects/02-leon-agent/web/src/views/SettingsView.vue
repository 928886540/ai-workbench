<script setup lang="ts">
import { LogOut, RefreshCw } from "@lucide/vue";
import { computed, onMounted, ref, watch } from "vue";
import { ApiError, api, type ModelSettingsResponse } from "../api/client";
import VoiceSettings from "../components/VoiceSettings.vue";

const props = defineProps<{ sessionId: string }>();
const emit = defineEmits<{ logout: [] }>();

const loading = ref(true);
const saving = ref(false);
const status = ref("");
const statusTone = ref<"neutral" | "error" | "ok">("neutral");
const modelInput = ref("");
const models = ref<string[]>([]);
const defaultModel = ref("");
const activeModel = ref("");
const provider = ref("");
const baseUrl = ref("");
const catalogError = ref("");
const listOpen = ref(false);

const filteredModels = computed(() => {
  const query = modelInput.value.trim().toLocaleLowerCase();
  return query
    ? models.value.filter((model) => model.toLocaleLowerCase().includes(query))
    : models.value;
});

const isCustomModel = computed(
  () => Boolean(modelInput.value.trim()) && !models.value.includes(modelInput.value.trim()),
);

function applySettings(data: ModelSettingsResponse): void {
  models.value = Array.isArray(data.models) ? data.models : [];
  defaultModel.value = data.default_model || "";
  activeModel.value = data.active_model || data.default_model || "";
  provider.value = data.provider || "";
  baseUrl.value = data.base_url || "";
  catalogError.value = data.catalog_error || "";
  modelInput.value = data.selected_model || "";
}

function setError(error: unknown, prefix: string): void {
  statusTone.value = "error";
  if (error instanceof ApiError && error.status === 401) {
    status.value = "Token 已失效，请重新登录";
  } else {
    status.value = `${prefix}：${error instanceof Error ? error.message : "未知错误"}`;
  }
}

async function loadSettings(refresh = false): Promise<void> {
  if (!props.sessionId) {
    loading.value = false;
    statusTone.value = "error";
    status.value = "会话尚未建立，请返回聊天页重试";
    return;
  }
  loading.value = true;
  status.value = refresh ? "正在刷新模型目录…" : "正在加载…";
  statusTone.value = "neutral";
  try {
    applySettings(await api.getModelSettings(props.sessionId, refresh));
    status.value = catalogError.value ? `目录拉取失败：${catalogError.value}` : "";
    statusTone.value = catalogError.value ? "error" : "neutral";
  } catch (error) {
    setError(error, "加载失败");
  } finally {
    loading.value = false;
  }
}

async function saveSettings(): Promise<void> {
  if (!props.sessionId) return;
  saving.value = true;
  statusTone.value = "neutral";
  status.value = "正在保存…";
  try {
    const model = modelInput.value.trim() || null;
    applySettings(await api.setModelSettings(props.sessionId, model));
    listOpen.value = false;
    statusTone.value = "ok";
    status.value = "已保存";
  } catch (error) {
    setError(error, "保存失败");
  } finally {
    saving.value = false;
  }
}

function chooseModel(model: string): void {
  modelInput.value = model;
  listOpen.value = true;
}

function handleInput(): void {
  listOpen.value = true;
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") listOpen.value = false;
}

watch(
  () => props.sessionId,
  () => {
    listOpen.value = false;
    void loadSettings();
  },
);

onMounted(() => void loadSettings());
</script>

<template>
  <section class="page-panel settings-panel" aria-labelledby="settings-title">
    <header class="page-panel__header">
      <h2 id="settings-title">设置</h2>
    </header>

    <article class="settings-card">
      <h3>模型</h3>
      <p class="settings-subtitle">当前：{{ activeModel || "未知" }}</p>

      <div class="settings-row">
        <input
          v-model="modelInput"
          type="text"
          autocomplete="off"
          spellcheck="false"
          :placeholder="defaultModel || '默认模型'"
          aria-label="模型 ID"
          :disabled="loading || saving"
          @focus="listOpen = true"
          @input="handleInput"
          @keydown="handleKeydown"
        />
        <button
          class="primary-small"
          type="button"
          :disabled="loading || saving"
          @click="void saveSettings()"
        >
          {{ saving ? "保存中…" : "保存" }}
        </button>
        <button
          class="icon-button icon-button--subtle"
          type="button"
          :disabled="loading"
          aria-label="刷新模型目录"
          title="刷新模型目录"
          @click="void loadSettings(true)"
        >
          <RefreshCw :class="{ spinning: loading }" :size="15" :stroke-width="2" aria-hidden="true" />
        </button>
      </div>

      <div v-if="listOpen" class="model-list" role="listbox" aria-label="可用模型">
        <button
          v-for="model in filteredModels"
          :key="model"
          class="model-option"
          :class="{ selected: model === modelInput }"
          type="button"
          role="option"
          :aria-selected="model === modelInput"
          @click="chooseModel(model)"
        >
          <span>{{ model }}</span>
          <small v-if="model === defaultModel">默认</small>
        </button>
        <p v-if="!filteredModels.length" class="model-empty">
          没有匹配项，可以直接保存自定义 ID。
        </p>
        <p v-if="isCustomModel" class="model-hint">
          “{{ modelInput.trim() }}” 不在目录中，保存后按自定义 ID 使用。
        </p>
      </div>

      <p v-if="status" class="settings-status" :data-tone="statusTone" role="status">{{ status }}</p>
    </article>

    <VoiceSettings />

    <button class="logout-big" type="button" @click="emit('logout')">
      <LogOut :size="18" :stroke-width="2" aria-hidden="true" />
      退出登录
    </button>
  </section>
</template>
