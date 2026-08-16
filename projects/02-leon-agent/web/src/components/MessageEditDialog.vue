<script setup lang="ts">
import { X } from "@lucide/vue";
import { nextTick, ref, watch } from "vue";

const props = defineProps<{
  open: boolean;
  modelValue: string;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: string];
  cancel: [];
  save: [];
}>();

const editor = ref<HTMLTextAreaElement | null>(null);

watch(
  () => props.open,
  (open) => {
    if (!open) return;
    void nextTick(() => {
      editor.value?.focus();
      editor.value?.setSelectionRange(props.modelValue.length, props.modelValue.length);
    });
  },
);
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="message-edit-overlay"
      role="presentation"
      @click.self="emit('cancel')"
    >
      <section
        class="message-edit-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="message-edit-title"
        @keydown.escape.prevent="emit('cancel')"
      >
        <header class="message-edit-dialog__header">
          <h2 id="message-edit-title">编辑消息</h2>
          <button type="button" aria-label="关闭编辑" @click="emit('cancel')">
            <X :size="19" :stroke-width="2" aria-hidden="true" />
          </button>
        </header>
        <textarea
          ref="editor"
          class="message-edit-dialog__textarea"
          :value="modelValue"
          rows="8"
          maxlength="8000"
          aria-label="消息内容"
          @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
          @keydown.ctrl.enter.prevent="emit('save')"
          @keydown.meta.enter.prevent="emit('save')"
        ></textarea>
        <footer class="message-edit-dialog__actions">
          <span>Ctrl / ⌘ + Enter 保存</span>
          <div>
            <button type="button" @click="emit('cancel')">取消</button>
            <button class="primary-small" type="button" @click="emit('save')">保存</button>
          </div>
        </footer>
      </section>
    </div>
  </Teleport>
</template>
