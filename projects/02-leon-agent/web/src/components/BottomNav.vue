<script setup lang="ts">
import { Images, ListTodo, MessageCircle, Settings } from "@lucide/vue";

export type WorkbenchView = "chat" | "tasks" | "gallery" | "settings";

defineProps<{ active: WorkbenchView }>();
const emit = defineEmits<{ select: [view: WorkbenchView] }>();

const items = [
  { id: "chat" as const, icon: MessageCircle, label: "聊天" },
  { id: "tasks" as const, icon: ListTodo, label: "任务" },
  { id: "gallery" as const, icon: Images, label: "图库" },
  { id: "settings" as const, icon: Settings, label: "设置" },
];
</script>

<template>
  <nav class="bottom-nav" aria-label="主导航">
    <button
      v-for="item in items"
      :key="item.id"
      class="bottom-nav__item"
      :class="{ active: active === item.id }"
      type="button"
      :aria-current="active === item.id ? 'page' : undefined"
      @click="emit('select', item.id as WorkbenchView)"
    >
      <component :is="item.icon" class="bottom-nav__icon" :size="18" :stroke-width="2" aria-hidden="true" />
      <span>{{ item.label }}</span>
    </button>
  </nav>
</template>
