<script setup lang="ts">
defineProps<{
  label: string;
  detail?: string;
  tone?: "neutral" | "ok" | "error";
}>();
</script>

<template>
  <div class="app-status" :data-tone="tone ?? 'neutral'">
    <span class="app-status__dot" aria-hidden="true"></span>
    <span>{{ label }}</span>
    <small v-if="detail">{{ detail }}</small>
  </div>
</template>
