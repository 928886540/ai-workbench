import { ref } from "vue";
import { api, type VoiceCatalogResponse } from "../api/client";

export interface VoiceOption {
  id: string;
  name: string;
  model: string;
  languages: string[];
  demo: string;
}

const PREFS_KEY = "leon_voice_prefs";

export const voiceEnabled = ref(false);
export const voiceModels = ref<Array<Record<string, unknown>>>([]);
export const voices = ref<VoiceOption[]>([]);
export const defaultVoiceId = ref("");
export const selectedVoiceId = ref("");
export const favoriteVoiceIds = ref<string[]>([]);
export const autoplayAll = ref(false);
export const voiceCatalogLoaded = ref(false);
export const voiceCatalogLoading = ref(false);

let catalogRequest: Promise<boolean> | null = null;

function readPrefs(): void {
  if (typeof localStorage === "undefined") return;
  try {
    const prefs = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") as {
      voiceId?: unknown;
      favorites?: unknown;
      autoplayAll?: unknown;
    };
    selectedVoiceId.value = typeof prefs.voiceId === "string" ? prefs.voiceId : "";
    favoriteVoiceIds.value = Array.isArray(prefs.favorites)
      ? prefs.favorites.filter((item): item is string => typeof item === "string")
      : [];
    autoplayAll.value = prefs.autoplayAll === true;
  } catch {
    selectedVoiceId.value = "";
    favoriteVoiceIds.value = [];
    autoplayAll.value = false;
  }
}

function persistPrefs(): void {
  if (typeof localStorage === "undefined") return;
  localStorage.setItem(
    PREFS_KEY,
    JSON.stringify({
      voiceId: selectedVoiceId.value,
      favorites: favoriteVoiceIds.value,
      autoplayAll: autoplayAll.value,
    }),
  );
}

readPrefs();

export function setVoiceCatalog(payload: {
  enabled: boolean;
  default_voice_id: string;
  models: Array<Record<string, unknown>>;
  voices: Array<{
    id: string;
    name?: string | null;
    model?: string | null;
    languages?: string[];
    demo?: string | null;
  }>;
}): void {
  voiceEnabled.value = payload.enabled;
  voiceModels.value = payload.models || [];
  voices.value = (payload.voices || []).map((voice) => ({
    id: voice.id,
    name: voice.name || voice.id,
    model: voice.model || "",
    languages: voice.languages || [],
    demo: voice.demo || "",
  }));
  defaultVoiceId.value = payload.default_voice_id || "";
  voiceCatalogLoaded.value = true;
  if (selectedVoiceId.value && !voices.value.some((voice) => voice.id === selectedVoiceId.value)) {
    selectedVoiceId.value = "";
    persistPrefs();
  }
}

/** Load the catalog once per app session so chat and settings share one request. */
export function loadVoiceCatalog(refresh = false): Promise<boolean> {
  if (!refresh && voiceCatalogLoaded.value) return Promise.resolve(voiceEnabled.value);
  if (catalogRequest) return catalogRequest;

  voiceCatalogLoading.value = true;
  catalogRequest = api
    .getVoiceCatalog(refresh)
    .then((payload: VoiceCatalogResponse) => {
      setVoiceCatalog(payload);
      return payload.enabled;
    })
    .finally(() => {
      voiceCatalogLoading.value = false;
      catalogRequest = null;
    });
  return catalogRequest;
}

export function clearVoiceCatalog(): void {
  voiceEnabled.value = false;
  voiceModels.value = [];
  voices.value = [];
  defaultVoiceId.value = "";
  voiceCatalogLoaded.value = false;
}

export function activeVoiceId(): string {
  return selectedVoiceId.value || defaultVoiceId.value;
}

export function activeVoice(): VoiceOption | null {
  const id = activeVoiceId();
  return voices.value.find((voice) => voice.id === id) || null;
}

export function isFavoriteVoice(id: string): boolean {
  return favoriteVoiceIds.value.includes(id);
}

export function selectVoice(id: string): void {
  selectedVoiceId.value = id;
  persistPrefs();
}

export function toggleFavoriteVoice(id: string): void {
  const index = favoriteVoiceIds.value.indexOf(id);
  if (index >= 0) favoriteVoiceIds.value.splice(index, 1);
  else favoriteVoiceIds.value.unshift(id);
  persistPrefs();
}

export function setAutoplayAll(value: boolean): void {
  autoplayAll.value = value;
  persistPrefs();
}
