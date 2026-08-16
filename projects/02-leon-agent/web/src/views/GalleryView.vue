<script setup lang="ts">
import { computed, ref } from "vue";
import ImageViewer, { type ViewerImage } from "../components/ImageViewer.vue";
import { galleryImages } from "../stores/images";

defineProps<{
  loading: boolean;
  error: string;
}>();

const viewerIndex = ref(-1);
const viewerItems = computed<ViewerImage[]>(() =>
  galleryImages.value.map((image) => ({ url: image.url, label: image.sourceText })),
);

function openViewer(jobId: string): void {
  viewerIndex.value = galleryImages.value.findIndex((image) => image.jobId === jobId);
}

function closeViewer(): void {
  viewerIndex.value = -1;
}
</script>

<template>
  <section class="page-panel" aria-label="图库">
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

    <ImageViewer
      v-if="viewerIndex >= 0"
      v-model:index="viewerIndex"
      :items="viewerItems"
      @close="closeViewer"
    />
  </section>
</template>
