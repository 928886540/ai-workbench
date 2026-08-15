<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { galleryImages } from "../stores/images";

defineProps<{
  loading: boolean;
  error: string;
}>();

const emit = defineEmits<{ refresh: [] }>();
const selectedJobId = ref("");
const viewer = ref<HTMLElement | null>(null);
const selectedIndex = computed(() =>
  galleryImages.value.findIndex((image) => image.jobId === selectedJobId.value),
);
const selectedImage = computed(() =>
  selectedIndex.value >= 0 ? galleryImages.value[selectedIndex.value] : null,
);

function openViewer(jobId: string): void {
  selectedJobId.value = jobId;
  void nextTick(() => viewer.value?.focus());
}

function closeViewer(): void {
  selectedJobId.value = "";
}

function moveViewer(offset: number): void {
  const count = galleryImages.value.length;
  if (!count) return;
  const current = selectedIndex.value >= 0 ? selectedIndex.value : 0;
  selectedJobId.value = galleryImages.value[(current + offset + count) % count].jobId;
}

function handleKeydown(event: KeyboardEvent): void {
  if (!selectedImage.value) return;
  if (event.key === "Escape") closeViewer();
  else if (event.key === "ArrowLeft") moveViewer(-1);
  else if (event.key === "ArrowRight") moveViewer(1);
}

onMounted(() => window.addEventListener("keydown", handleKeydown));
onBeforeUnmount(() => window.removeEventListener("keydown", handleKeydown));
</script>

<template>
  <section class="page-panel" aria-labelledby="gallery-title">
    <header class="page-panel__header">
      <div>
        <p class="eyebrow">IMAGE GALLERY</p>
        <h2 id="gallery-title">图库</h2>
      </div>
      <button class="ghost-button" type="button" :disabled="loading" @click="emit('refresh')">
        {{ loading ? "刷新中…" : "刷新" }}
      </button>
    </header>

    <p v-if="error" class="panel-error">{{ error }}</p>
    <div v-if="loading && !galleryImages.length" class="panel-empty">正在加载图库…</div>
    <div v-else-if="!galleryImages.length" class="panel-empty">
      <strong>暂无图片</strong>
      <span>完成的生图会自动出现在这里。</span>
    </div>
    <div v-else class="gallery-grid">
      <button
        v-for="image in galleryImages"
        :key="image.jobId"
        class="gallery-card"
        type="button"
        :aria-label="`查看 ${image.sourceText || image.jobId}`"
        @click="openViewer(image.jobId)"
      >
        <img :src="image.url" :alt="image.sourceText || '生成图片'" loading="lazy" />
        <span>{{ image.sourceText || image.jobId.slice(0, 12) + "…" }}</span>
      </button>
    </div>

    <div
      v-if="selectedImage"
      ref="viewer"
      class="image-viewer"
      tabindex="0"
      role="dialog"
      aria-modal="true"
      aria-label="图片预览"
      @click.self="closeViewer"
    >
      <button class="image-viewer__close" type="button" aria-label="关闭" @click="closeViewer">
        ×
      </button>
      <button
        v-if="galleryImages.length > 1"
        class="image-viewer__nav image-viewer__nav--prev"
        type="button"
        aria-label="上一张"
        @click="moveViewer(-1)"
      >
        ‹
      </button>
      <figure class="image-viewer__figure">
        <img :src="selectedImage.url" :alt="selectedImage.sourceText || '生成图片'" />
        <figcaption>
          {{ selectedIndex + 1 }} / {{ galleryImages.length }}
          <span v-if="selectedImage.sourceText"> · {{ selectedImage.sourceText }}</span>
        </figcaption>
      </figure>
      <button
        v-if="galleryImages.length > 1"
        class="image-viewer__nav image-viewer__nav--next"
        type="button"
        aria-label="下一张"
        @click="moveViewer(1)"
      >
        ›
      </button>
    </div>
  </section>
</template>
