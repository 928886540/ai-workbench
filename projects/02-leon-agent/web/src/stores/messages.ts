import { computed, ref } from "vue";

export type MessageRole = "user" | "agent" | "system";
export type MessageStatus = "pending" | "streaming" | "done" | "error";

export interface ChatMessage {
  id: string;
  role: MessageRole;
  text: string;
  status: MessageStatus;
  images: string[];
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
// have to recreate the legacy DOM state machine.
export const messages = ref<ChatMessage[]>([]);
export const messageIndex = computed(
  () => new Map(messages.value.map((message) => [message.id, message])),
);

export function upsertMessage(message: ChatMessage): void {
  const index = messages.value.findIndex((item) => item.id === message.id);
  if (index === -1) {
    messages.value.push(message);
    return;
  }
  messages.value[index] = message;
}
