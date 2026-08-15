<script setup lang="ts">
export type WorkbenchView = "chat" | "tasks" | "gallery";

defineProps<{ active: WorkbenchView }>();
const emit = defineEmits<{ select: [view: WorkbenchView] }>();
</script>

<template>
  <nav class="bottom-nav" aria-label="主导航">
    <button
      v-for="item in [
        { id: 'chat', icon: '💬', label: '聊天' },
        { id: 'tasks', icon: '📋', label: '任务' },
        { id: 'gallery', icon: '🖼️', label: '图库' },
      ]"
      :key="item.id"
      class="bottom-nav__item"
      :class="{ active: active === item.id }"
      type="button"
      :aria-current="active === item.id ? 'page' : undefined"
      @click="emit('select', item.id as WorkbenchView)"
    >
      <span class="bottom-nav__icon" aria-hidden="true">{{ item.icon }}</span>
      <span>{{ item.label }}</span>
    </button>
  </nav>
</template>
