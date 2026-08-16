<script setup lang="ts">
import { imageTasks } from "../stores/images";

defineProps<{
  loading: boolean;
  error: string;
}>();

function statusKey(status: string): string {
  const value = status.trim().toLowerCase();
  if (["running", "processing", "in_progress"].includes(value)) return "running";
  if (["completed", "success", "done"].includes(value)) return "completed";
  if (["failed", "error"].includes(value)) return "failed";
  if (["cancelled", "canceled"].includes(value)) return "cancelled";
  return "queued";
}

function statusLabel(status: string): string {
  return {
    queued: "排队中",
    running: "生成中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  }[statusKey(status)] || "未知";
}

function progressLabel(progress: number | null): string {
  if (progress === null) return "";
  const value = Math.max(0, Math.min(100, Math.round(progress)));
  return `${value}%`;
}
</script>

<template>
  <section class="page-panel" aria-label="生图任务">
    <p v-if="error" class="panel-error">{{ error }}</p>
    <div v-if="loading && !imageTasks.length" class="panel-empty">正在加载任务…</div>
    <div v-else-if="!imageTasks.length" class="panel-empty">
      <strong>暂无任务</strong>
      <span>发送生图请求后，任务会出现在这里。</span>
    </div>
    <div v-else class="task-list">
      <article v-for="task in imageTasks" :key="task.jobId" class="task-card">
        <div class="task-card__top">
          <strong>{{ task.modeName || task.modeId || task.jobId }}</strong>
          <span class="status-badge" :data-status="statusKey(task.status)">
            {{ statusLabel(task.status) }}
          </span>
        </div>
        <p v-if="task.sourceText" class="task-card__source">{{ task.sourceText }}</p>
        <div v-if="task.progress !== null" class="task-card__progress">
          <span>进度 {{ progressLabel(task.progress) }}</span>
          <progress :value="task.progress" max="100"></progress>
        </div>
        <details class="task-card__details">
          <summary>任务详情</summary>
          <code>任务 ID：{{ task.jobId }}</code>
          <code v-if="task.generationPlanId">生成计划 ID：{{ task.generationPlanId }}</code>
        </details>
      </article>
    </div>
  </section>
</template>
