import { computed, ref } from "vue";

export type MessageRole = "user" | "agent" | "system";
export type MessageStatus = "pending" | "streaming" | "done" | "error" | "cancelled";
export type MessageKind = "message" | "image-result";
export type ToolCallStatus = "running" | "done" | "error" | "cancelled";

export interface MessageToolCall {
  id: string;
  name: string;
  status: ToolCallStatus;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
}

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
  /** Only a newly delivered SSE clip should attempt autoplay. */
  autoplay: boolean;
}

export interface MessageRevision {
  kind: MessageKind;
  text: string;
  status: MessageStatus;
  images: string[];
  tools: MessageToolCall[];
  audio?: VoiceClip;
  voice?: VoiceClip;
  meta: ChatMessage["meta"];
}

export interface ChatMessage {
  id: string;
  role: MessageRole;
  kind: MessageKind;
  text: string;
  status: MessageStatus;
  images: string[];
  tools: MessageToolCall[];
  revisions: MessageRevision[];
  revisionIndex: number;
  /** Canonical payload for an agent-initiated voice message. */
  audio?: VoiceClip;
  /** Alias retained for callers that refer to the payload as a voice clip. */
  voice?: VoiceClip;
  /** Epoch ms when the message was sent/persisted; null when unknown. */
  createdAt: number | null;
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
    kind: "message",
    text,
    status,
    images: [],
    tools: [],
    revisions: [],
    revisionIndex: 0,
    createdAt: now,
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

export function beginMessageRevision(message: ChatMessage): void {
  message.revisions.push({
    kind: message.kind,
    text: message.text,
    status: message.status,
    images: [...message.images],
    tools: message.tools.map((tool) => ({
      ...tool,
      input: { ...tool.input },
      output: { ...tool.output },
    })),
    audio: message.audio ? { ...message.audio } : undefined,
    voice: message.voice ? { ...message.voice } : undefined,
    meta: { ...message.meta },
  });
  const now = Date.now();
  message.kind = "message";
  message.text = "";
  message.status = "pending";
  message.images = [];
  message.tools = [];
  message.audio = undefined;
  message.voice = undefined;
  message.revisionIndex = message.revisions.length;
  message.meta = {
    model: null,
    startedAt: now,
    finishedAt: null,
    elapsedMs: null,
    tokensIn: null,
    tokensOut: null,
  };
}

export function cycleMessageRevision(message: ChatMessage): void {
  const count = message.revisions.length + 1;
  message.revisionIndex = (message.revisionIndex + 1) % count;
}

export function removeMessage(id: string): void {
  const index = messages.value.findIndex((message) => message.id === id);
  if (index >= 0) messages.value.splice(index, 1);
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
