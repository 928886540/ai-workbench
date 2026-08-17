<script setup lang="ts">
import { ChevronLeft, ChevronRight, RotateCcw, X, ZoomIn, ZoomOut } from "@lucide/vue";
import { computed, nextTick, onMounted, ref, watch } from "vue";

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
const imageElement = ref<HTMLImageElement | null>(null);
const scale = ref(1);
const translateX = ref(0);
const translateY = ref(0);
const gesturing = ref(false);
const activeIndex = computed(() => {
  if (!props.items.length) return 0;
  return Math.min(Math.max(props.index, 0), props.items.length - 1);
});
const activeImage = computed(() => props.items[activeIndex.value] || null);
const imageTransform = computed(() => ({
  transform: `translate3d(${translateX.value}px, ${translateY.value}px, 0) scale(${scale.value})`,
}));

const MIN_SCALE = 1;
const MAX_SCALE = 5;
const ZOOM_STEP = 1.5;

interface PointerPosition {
  x: number;
  y: number;
}

interface DragStart extends PointerPosition {
  pointerId: number;
  translateX: number;
  translateY: number;
}

interface PinchStart {
  distance: number;
  scale: number;
  contentX: number;
  contentY: number;
}

const activePointers = new Map<number, PointerPosition>();
let dragStart: DragStart | null = null;
let pinchStart: PinchStart | null = null;
let gestureWasPinch = false;

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function imageBaseSize(): { width: number; height: number; viewportWidth: number; viewportHeight: number } {
  const host = viewer.value;
  const image = imageElement.value;
  const viewportWidth = host?.clientWidth || window.innerWidth;
  const viewportHeight = host?.clientHeight || window.innerHeight;
  if (!image?.naturalWidth || !image.naturalHeight) {
    return { width: viewportWidth, height: viewportHeight, viewportWidth, viewportHeight };
  }
  const imageRatio = image.naturalWidth / image.naturalHeight;
  const viewportRatio = viewportWidth / viewportHeight;
  if (imageRatio > viewportRatio) {
    return {
      width: viewportWidth,
      height: viewportWidth / imageRatio,
      viewportWidth,
      viewportHeight,
    };
  }
  return {
    width: viewportHeight * imageRatio,
    height: viewportHeight,
    viewportWidth,
    viewportHeight,
  };
}

function constrainedTranslation(
  nextX: number,
  nextY: number,
  nextScale = scale.value,
): { x: number; y: number } {
  const size = imageBaseSize();
  const maxX = Math.max(0, (size.width * nextScale - size.viewportWidth) / 2);
  const maxY = Math.max(0, (size.height * nextScale - size.viewportHeight) / 2);
  return {
    x: clamp(nextX, -maxX, maxX),
    y: clamp(nextY, -maxY, maxY),
  };
}

function applyTransform(nextScale: number, nextX: number, nextY: number): void {
  const normalizedScale = clamp(nextScale, MIN_SCALE, MAX_SCALE);
  const position = constrainedTranslation(nextX, nextY, normalizedScale);
  scale.value = normalizedScale;
  translateX.value = position.x;
  translateY.value = position.y;
}

function resetTransform(): void {
  scale.value = MIN_SCALE;
  translateX.value = 0;
  translateY.value = 0;
}

function zoomAt(nextScale: number, clientX: number, clientY: number): void {
  const host = viewer.value;
  if (!host) return;
  const normalizedScale = clamp(nextScale, MIN_SCALE, MAX_SCALE);
  if (normalizedScale <= MIN_SCALE) {
    resetTransform();
    return;
  }
  const rect = host.getBoundingClientRect();
  const localX = clientX - rect.left - rect.width / 2;
  const localY = clientY - rect.top - rect.height / 2;
  const contentX = (localX - translateX.value) / scale.value;
  const contentY = (localY - translateY.value) / scale.value;
  applyTransform(
    normalizedScale,
    localX - contentX * normalizedScale,
    localY - contentY * normalizedScale,
  );
}

function stepZoom(direction: 1 | -1): void {
  const host = viewer.value;
  if (!host) return;
  const nextScale = direction > 0 ? scale.value * ZOOM_STEP : scale.value / ZOOM_STEP;
  zoomAt(nextScale < 1.05 ? MIN_SCALE : nextScale, host.clientWidth / 2, host.clientHeight / 2);
}

function move(offset: number): void {
  const count = props.items.length;
  if (count <= 1) return;
  resetTransform();
  emit("update:index", (activeIndex.value + offset + count) % count);
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") emit("close");
  else if (event.key === "ArrowLeft") move(-1);
  else if (event.key === "ArrowRight") move(1);
}

function isControlTarget(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest("button"));
}

function rebaseDrag(pointerId: number, point: PointerPosition): void {
  dragStart = {
    pointerId,
    x: point.x,
    y: point.y,
    translateX: translateX.value,
    translateY: translateY.value,
  };
}

function pinchMetrics(): { distance: number; localX: number; localY: number } | null {
  const host = viewer.value;
  const points = [...activePointers.values()].slice(0, 2);
  if (!host || points.length < 2) return null;
  const [first, second] = points;
  const rect = host.getBoundingClientRect();
  return {
    distance: Math.hypot(second.x - first.x, second.y - first.y),
    localX: (first.x + second.x) / 2 - rect.left - rect.width / 2,
    localY: (first.y + second.y) / 2 - rect.top - rect.height / 2,
  };
}

function beginPinch(): void {
  const metrics = pinchMetrics();
  if (!metrics || metrics.distance <= 0) return;
  pinchStart = {
    distance: metrics.distance,
    scale: scale.value,
    contentX: (metrics.localX - translateX.value) / scale.value,
    contentY: (metrics.localY - translateY.value) / scale.value,
  };
  gestureWasPinch = true;
}

function handlePointerDown(event: PointerEvent): void {
  if (isControlTarget(event.target)) return;
  if (event.pointerType === "mouse" && event.button !== 0) return;
  if (activePointers.size === 0) gestureWasPinch = false;
  const point = { x: event.clientX, y: event.clientY };
  activePointers.set(event.pointerId, point);
  gesturing.value = true;
  try {
    viewer.value?.setPointerCapture?.(event.pointerId);
  } catch {
    // Synthetic browser checks may not register an active native pointer.
  }
  if (activePointers.size === 1) rebaseDrag(event.pointerId, point);
  else beginPinch();
}

function handlePointerMove(event: PointerEvent): void {
  if (!activePointers.has(event.pointerId)) return;
  const point = { x: event.clientX, y: event.clientY };
  activePointers.set(event.pointerId, point);
  if (activePointers.size >= 2) {
    if (!pinchStart) beginPinch();
    const metrics = pinchMetrics();
    if (!pinchStart || !metrics) return;
    const nextScale = clamp(
      pinchStart.scale * (metrics.distance / pinchStart.distance),
      MIN_SCALE,
      MAX_SCALE,
    );
    applyTransform(
      nextScale,
      metrics.localX - pinchStart.contentX * nextScale,
      metrics.localY - pinchStart.contentY * nextScale,
    );
    return;
  }
  if (scale.value <= MIN_SCALE || !dragStart || dragStart.pointerId !== event.pointerId) return;
  applyTransform(
    scale.value,
    dragStart.translateX + event.clientX - dragStart.x,
    dragStart.translateY + event.clientY - dragStart.y,
  );
}

function releasePointer(event: PointerEvent): void {
  try {
    if (viewer.value?.hasPointerCapture?.(event.pointerId)) {
      viewer.value.releasePointerCapture(event.pointerId);
    }
  } catch {
    // The pointer may already have been released by the browser.
  }
}

function finishPointer(event: PointerEvent, cancelled: boolean): void {
  if (!activePointers.has(event.pointerId)) return;
  const completedDrag = dragStart?.pointerId === event.pointerId ? dragStart : null;
  releasePointer(event);
  activePointers.delete(event.pointerId);

  if (activePointers.size >= 2) {
    beginPinch();
    return;
  }
  if (activePointers.size === 1) {
    const [remainingId, remainingPoint] = activePointers.entries().next().value as [
      number,
      PointerPosition,
    ];
    pinchStart = null;
    rebaseDrag(remainingId, remainingPoint);
    return;
  }

  gesturing.value = false;
  pinchStart = null;
  dragStart = null;
  if (scale.value > MIN_SCALE) {
    applyTransform(scale.value, translateX.value, translateY.value);
    return;
  }
  resetTransform();
  if (cancelled || gestureWasPinch || !completedDrag) return;
  const deltaX = event.clientX - completedDrag.x;
  const deltaY = event.clientY - completedDrag.y;
  if (Math.abs(deltaX) < 48 || Math.abs(deltaX) <= Math.abs(deltaY) * 1.15) return;
  move(deltaX < 0 ? 1 : -1);
}

function handlePointerUp(event: PointerEvent): void {
  finishPointer(event, false);
}

function handlePointerCancel(event: PointerEvent): void {
  finishPointer(event, true);
}

function handleWheel(event: WheelEvent): void {
  const factor = event.deltaY < 0 ? 1.18 : 1 / 1.18;
  zoomAt(scale.value * factor, event.clientX, event.clientY);
}

function handleDoubleClick(event: MouseEvent): void {
  if (scale.value > MIN_SCALE) resetTransform();
  else zoomAt(2.5, event.clientX, event.clientY);
}

watch(() => activeImage.value?.url, resetTransform);
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
      :data-zoomed="scale > 1"
      @click.self="emit('close')"
      @keydown="handleKeydown"
      @pointerdown="handlePointerDown"
      @pointermove="handlePointerMove"
      @pointerup="handlePointerUp"
      @pointercancel="handlePointerCancel"
      @wheel.prevent="handleWheel"
    >
      <button class="image-viewer__close" type="button" aria-label="关闭" @click="emit('close')">
        <X :size="22" :stroke-width="2" aria-hidden="true" />
      </button>
      <div class="image-viewer__zoom-controls" aria-label="图片缩放控制">
        <button type="button" aria-label="缩小" :disabled="scale <= 1" @click="stepZoom(-1)">
          <ZoomOut :size="20" :stroke-width="1.9" aria-hidden="true" />
        </button>
        <button type="button" aria-label="重置缩放" :disabled="scale <= 1" @click="resetTransform">
          <RotateCcw :size="19" :stroke-width="1.9" aria-hidden="true" />
        </button>
        <button type="button" aria-label="放大" :disabled="scale >= 5" @click="stepZoom(1)">
          <ZoomIn :size="20" :stroke-width="1.9" aria-hidden="true" />
        </button>
      </div>
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
          ref="imageElement"
          :key="`${activeIndex}:${activeImage.url}`"
          :src="activeImage.url"
          :alt="activeImage.label || '生成图片'"
          :class="{ 'is-gesturing': gesturing }"
          :style="imageTransform"
          draggable="false"
          @dblclick.stop="handleDoubleClick"
          @dragstart.prevent
          @load="applyTransform(scale, translateX, translateY)"
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
