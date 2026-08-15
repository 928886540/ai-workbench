<script setup lang="ts">
import { onMounted, ref } from "vue";
import AppStatus from "../components/AppStatus.vue";
import { fetchHealth } from "../api/client";

const status = ref<"checking" | "ready" | "error">("checking");
const detail = ref("正在连接 Gateway…");

onMounted(async () => {
  try {
    const health = await fetchHealth();
    status.value = health.ok ? "ready" : "error";
    detail.value = health.ok ? "API 已连通" : "Gateway 返回异常";
  } catch (error) {
    status.value = "error";
    detail.value = error instanceof Error ? error.message : "无法连接 Gateway";
  }
});
</script>

<template>
  <main class="migration-shell">
    <header class="migration-header">
      <div>
        <p class="eyebrow">LEON AGENT · VUE 3</p>
        <h1>聊天工作台迁移基座</h1>
        <p class="subtitle">消息状态、API 和视图边界已经就位，旧版页面仍可随时回退。</p>
      </div>
      <AppStatus
        :label="status === 'checking' ? '检查中' : status === 'ready' ? 'Gateway 正常' : 'Gateway 异常'"
        :detail="detail"
        :tone="status === 'checking' ? 'neutral' : status === 'ready' ? 'ok' : 'error'"
      />
    </header>

    <section class="migration-card" aria-labelledby="migration-title">
      <span class="migration-card__badge">W2</span>
      <h2 id="migration-title">Vue 3 + Vite 构建链路已接通</h2>
      <p>
        这是迁移期间的安全入口。功能页面会按视图逐步搬迁，完成验收前不会替换现有手机端。
      </p>
      <code>LEON_WEB_CLIENT=vue</code>
    </section>
  </main>
</template>
