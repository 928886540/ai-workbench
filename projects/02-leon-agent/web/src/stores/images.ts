import { ref } from "vue";

export interface ImageTask {
  jobId: string;
  status: string;
  progress: number | null;
  generationPlanId: string;
  modeId: string;
  modeName: string;
  sourceText: string;
  imageUrl: string;
  createdAt: number;
}

export interface GalleryImage {
  jobId: string;
  url: string;
  sourceText: string;
  createdAt: number;
}

export const imageTasks = ref<ImageTask[]>([]);
export const galleryImages = ref<GalleryImage[]>([]);

function asString(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function asNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function asTimestamp(value: unknown, fallback: number): number {
  const numeric = asNumber(value);
  if (numeric !== null && numeric > 0) return numeric < 1e12 ? numeric * 1000 : numeric;
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function sortNewestFirst<T extends { createdAt: number }>(items: T[]): void {
  items.sort((left, right) => right.createdAt - left.createdAt);
}

export function clearImageState(): void {
  imageTasks.value.splice(0, imageTasks.value.length);
  galleryImages.value.splice(0, galleryImages.value.length);
}

export function upsertImageTask(
  raw: Record<string, unknown>,
  fallbackTimestamp = Date.now(),
): ImageTask | null {
  const jobId = asString(raw.job_id ?? raw.jobId).trim();
  if (!jobId) return null;
  const index = imageTasks.value.findIndex((item) => item.jobId === jobId);
  const previous = index >= 0 ? imageTasks.value[index] : null;
  const task: ImageTask = {
    jobId,
    status: asString(raw.status) || previous?.status || "queued",
    progress: asNumber(raw.progress) ?? previous?.progress ?? null,
    generationPlanId:
      asString(raw.generation_plan_id ?? raw.generationPlanId) ||
      previous?.generationPlanId ||
      "",
    modeId:
      asString(raw.mode_id ?? raw.modeId ?? raw.workflow_name) || previous?.modeId || "",
    modeName: asString(raw.mode_name ?? raw.modeName) || previous?.modeName || "",
    sourceText: asString(raw.source_text ?? raw.sourceText) || previous?.sourceText || "",
    imageUrl: asString(raw.image_url ?? raw.imageUrl) || previous?.imageUrl || "",
    createdAt: asTimestamp(
      raw.created_at ?? raw.createdAt,
      previous?.createdAt || fallbackTimestamp,
    ),
  };
  if (index >= 0) imageTasks.value[index] = task;
  else imageTasks.value.push(task);
  sortNewestFirst(imageTasks.value);
  return task;
}

export function upsertGalleryImage(
  raw: Record<string, unknown>,
  fallbackTimestamp = Date.now(),
): GalleryImage | null {
  const jobId = asString(raw.job_id ?? raw.jobId).trim();
  const url = asString(raw.image_url ?? raw.imageUrl ?? raw.url).trim();
  if (!jobId || !url) return null;
  const index = galleryImages.value.findIndex((item) => item.jobId === jobId);
  const previous = index >= 0 ? galleryImages.value[index] : null;
  const image: GalleryImage = {
    jobId,
    url,
    sourceText: asString(raw.source_text ?? raw.sourceText) || previous?.sourceText || "",
    createdAt: asTimestamp(
      raw.created_at ?? raw.createdAt,
      previous?.createdAt || fallbackTimestamp,
    ),
  };
  if (index >= 0) galleryImages.value[index] = image;
  else galleryImages.value.push(image);
  sortNewestFirst(galleryImages.value);
  return image;
}

export function hydrateImageState(
  tasks: Array<Record<string, unknown>>,
  images: Array<Record<string, unknown>>,
): void {
  for (const task of tasks) {
    const normalized = upsertImageTask(task, 0);
    if (normalized?.imageUrl) {
      upsertGalleryImage(
        {
          job_id: normalized.jobId,
          image_url: normalized.imageUrl,
          source_text: normalized.sourceText,
          created_at: normalized.createdAt,
        },
        normalized.createdAt,
      );
    }
  }
  for (const image of images) upsertGalleryImage(image, 0);
}

export function completeImageTask(
  jobId: string,
  imageUrl: string,
  timestamp = Date.now(),
): void {
  const previous = imageTasks.value.find((item) => item.jobId === jobId);
  const task = upsertImageTask(
    { job_id: jobId, status: "completed", image_url: imageUrl },
    previous?.createdAt || timestamp,
  );
  upsertGalleryImage(
    {
      job_id: jobId,
      image_url: imageUrl,
      source_text: task?.sourceText || "",
      created_at: task?.createdAt || timestamp,
    },
    timestamp,
  );
}
