<script setup lang="ts">
import { ChevronLeft, ChevronRight, X } from "@lucide/vue";
import { computed, nextTick, onMounted, ref } from "vue";

export interface ViewerImage {
  url: string;
  label?: string;
}

const props = defineProps<{
  items: ViewerImage[];
  index: number;
}>();

const emit = defineEmits<{
  close: [];
  "update:index": [index: number];
}>();

const viewer = ref<HTMLElement | null>(null);
const activeIndex = computed(() => {
  if (!props.items.length) return 0;
  return Math.min(Math.max(props.index, 0), props.items.length - 1);
});
const activeImage = computed(() => props.items[activeIndex.value] || null);

let pointerId: number | null = null;
let pointerStartX = 0;
let pointerStartY = 0;

function move(offset: number): void {
  const count = props.items.length;
  if (count <= 1) return;
  emit("update:index", (activeIndex.value + offset + count) % count);
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") emit("close");
  else if (event.key === "ArrowLeft") move(-1);
  else if (event.key === "ArrowRight") move(1);
}

function handlePointerDown(event: PointerEvent): void {
  if (event.pointerType === "mouse" && event.button !== 0) return;
  pointerId = event.pointerId;
  pointerStartX = event.clientX;
  pointerStartY = event.clientY;
  try {
    viewer.value?.setPointerCapture?.(event.pointerId);
  } catch {
    // Synthetic browser checks may not register an active native pointer.
  }
}

function clearPointer(event: PointerEvent): void {
  if (pointerId !== event.pointerId) return;
  try {
    if (viewer.value?.hasPointerCapture?.(event.pointerId)) {
      viewer.value.releasePointerCapture(event.pointerId);
    }
  } catch {
    // The pointer may already have been released by the browser.
  }
  pointerId = null;
}

function handlePointerUp(event: PointerEvent): void {
  if (pointerId !== event.pointerId) return;
  const deltaX = event.clientX - pointerStartX;
  const deltaY = event.clientY - pointerStartY;
  clearPointer(event);
  if (Math.abs(deltaX) < 48 || Math.abs(deltaX) <= Math.abs(deltaY) * 1.15) return;
  move(deltaX < 0 ? 1 : -1);
}

onMounted(() => void nextTick(() => viewer.value?.focus()));
</script>

<template>
  <Teleport to="body">
    <div
      v-if="activeImage"
      ref="viewer"
      class="image-viewer"
      tabindex="0"
      role="dialog"
      aria-modal="true"
      aria-label="图片预览"
      @click.self="emit('close')"
      @keydown="handleKeydown"
      @pointerdown="handlePointerDown"
      @pointerup="handlePointerUp"
      @pointercancel="clearPointer"
    >
      <button class="image-viewer__close" type="button" aria-label="关闭" @click="emit('close')">
        <X :size="22" :stroke-width="2" aria-hidden="true" />
      </button>
      <button
        v-if="items.length > 1"
        class="image-viewer__nav image-viewer__nav--prev"
        type="button"
        aria-label="上一张"
        @click="move(-1)"
      >
        <ChevronLeft :size="30" :stroke-width="1.8" aria-hidden="true" />
      </button>
      <figure class="image-viewer__figure">
        <img
          :key="`${activeIndex}:${activeImage.url}`"
          :src="activeImage.url"
          :alt="activeImage.label || '生成图片'"
          draggable="false"
          @dragstart.prevent
        />
        <figcaption v-if="items.length > 1 || activeImage.label">
          <span v-if="items.length > 1">{{ activeIndex + 1 }} / {{ items.length }}</span>
          <span v-if="activeImage.label">
            {{ items.length > 1 ? ` · ${activeImage.label}` : activeImage.label }}
          </span>
        </figcaption>
      </figure>
      <button
        v-if="items.length > 1"
        class="image-viewer__nav image-viewer__nav--next"
        type="button"
        aria-label="下一张"
        @click="move(1)"
      >
        <ChevronRight :size="30" :stroke-width="1.8" aria-hidden="true" />
      </button>
    </div>
  </Teleport>
</template>
