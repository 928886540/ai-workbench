import { computed, ref } from "vue";

export type MessageRole = "user" | "agent" | "system";
export type MessageStatus = "pending" | "streaming" | "done" | "error";

/**
 * An audio clip emitted by the agent's `speak_text` tool.
 *
 * The gateway uses snake_case on the wire; the store keeps a small normalized
 * object so components never have to know about the SSE payload shape.
 */
export interface VoiceClip {
  clipId: string;
  url: string;
  text: string;
  voiceId: string | null;
  voiceName: string;
  bytes: number;
}

export interface ChatMessage {
  id: string;
  role: MessageRole;
  text: string;
  status: MessageStatus;
  images: string[];
  /** Canonical payload for an agent-initiated voice message. */
  audio?: VoiceClip;
  /** Alias retained for callers that refer to the payload as a voice clip. */
  voice?: VoiceClip;
  meta: {
    model: string | null;
    startedAt: number | null;
    finishedAt: number | null;
    elapsedMs: number | null;
    tokensIn: number | null;
    tokensOut: number | null;
  };
}

// W2 keeps one reactive message source so the later component migration does not
// have to recreate the old DOM state machine.
export const messages = ref<ChatMessage[]>([]);
export const messageIndex = computed(
  () => new Map(messages.value.map((message) => [message.id, message])),
);

export function makeMessage(
  role: MessageRole,
  text = "",
  status: MessageStatus = "done",
): ChatMessage {
  const now = Date.now();
  return {
    id: `m_${now}_${Math.random().toString(36).slice(2, 8)}`,
    role,
    text,
    status,
    images: [],
    meta: {
      model: null,
      startedAt: status === "pending" || status === "streaming" ? now : null,
      finishedAt: status === "done" || status === "error" ? now : null,
      elapsedMs: null,
      tokensIn: null,
      tokensOut: null,
    },
  };
}

export function appendMessage(message: ChatMessage): ChatMessage {
  messages.value.push(message);
  return message;
}

/** Insert a message before an existing message, or append when the anchor is gone. */
export function insertMessageBefore(
  message: ChatMessage,
  beforeId: string | null,
): ChatMessage {
  if (beforeId) {
    const index = messages.value.findIndex((item) => item.id === beforeId);
    if (index >= 0) {
      messages.value.splice(index, 0, message);
      return message;
    }
  }
  messages.value.push(message);
  return message;
}

export function hasVoiceClip(clipId: string): boolean {
  if (!clipId) return false;
  return messages.value.some(
    (message) =>
      message.audio?.clipId === clipId || message.voice?.clipId === clipId,
  );
}

export function clearMessages(): void {
  messages.value.splice(0, messages.value.length);
}

export function findMessage(id: string | null): ChatMessage | null {
  return id ? messageIndex.value.get(id) || null : null;
}

export function latestMessage(role?: MessageRole): ChatMessage | null {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const message = messages.value[index];
    if (!role || message.role === role) return message;
  }
  return null;
}

export function upsertMessage(message: ChatMessage): void {
  const index = messages.value.findIndex((item) => item.id === message.id);
  if (index === -1) {
    messages.value.push(message);
    return;
  }
  messages.value[index] = message;
}
