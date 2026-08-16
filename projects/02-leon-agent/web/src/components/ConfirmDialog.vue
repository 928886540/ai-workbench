<script setup lang="ts">
import { LogOut, X } from "@lucide/vue";
import { nextTick, ref, watch } from "vue";

const props = defineProps<{
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
}>();

const emit = defineEmits<{
  cancel: [];
  confirm: [];
}>();

const dialog = ref<HTMLElement | null>(null);

watch(
  () => props.open,
  (open) => {
    if (open) void nextTick(() => dialog.value?.focus());
  },
);
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="confirm-overlay" @click.self="emit('cancel')">
      <section
        ref="dialog"
        class="confirm-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="title"
        tabindex="-1"
        @keydown.escape.prevent="emit('cancel')"
      >
        <header class="confirm-dialog__header">
          <span class="confirm-dialog__icon" aria-hidden="true">
            <LogOut :size="20" :stroke-width="2" />
          </span>
          <div>
            <h2>{{ title }}</h2>
            <p>{{ description }}</p>
          </div>
          <button type="button" aria-label="关闭确认" @click="emit('cancel')">
            <X :size="19" :stroke-width="2" aria-hidden="true" />
          </button>
        </header>
        <footer class="confirm-dialog__actions">
          <button type="button" @click="emit('cancel')">取消</button>
          <button class="confirm-dialog__submit" type="button" @click="emit('confirm')">
            {{ confirmLabel }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>
