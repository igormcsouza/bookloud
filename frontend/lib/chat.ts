export type ChatDisabledReason = "NOT_CONFIGURED" | "NON_PROD";
export type FinishReason = "END_TURN" | "MAX_TOKENS" | "REFUSAL" | "DISABLED" | "ERROR" | "TRUNCATED";

export interface ChatUsage {
  inputTokens: number;
  outputTokens: number;
  cachedInputTokens: number;
}

export type ChatStreamEvent =
  | { type: "meta"; model: string | null; enabled: boolean; reason: ChatDisabledReason | null; anchorChunk: number; windowChunks: number[]; messageId: string }
  | { type: "delta"; text: string }
  | { type: "done"; finishReason: FinishReason; usage?: ChatUsage; messageId: string }
  | { type: "error"; code: string; message: string };

export interface AskRequest {
  question: string;
  anchorChunk: number | null;
  positionMs: number | null;
}

export function createSseParser(): (chunk: string) => ChatStreamEvent[] {
  let buffer = "";

  return (chunk: string): ChatStreamEvent[] => {
    buffer += chunk;
    const events: ChatStreamEvent[] = [];
    
    // We split on \n or \r\n, looking for complete frames
    // A complete frame is separated by an empty line
    const parts = buffer.split(/\r?\n\r?\n/);
    
    // The last part might be incomplete, put it back in buffer
    buffer = parts.pop() || "";

    for (const part of parts) {
      if (!part.trim()) continue;

      const lines = part.split(/\r?\n/);
      let eventType: string | null = null;
      let dataBuffer = "";

      for (const line of lines) {
        if (line.startsWith(":")) continue; // keepalive comment

        const colonIdx = line.indexOf(":");
        if (colonIdx === -1) continue;

        const field = line.slice(0, colonIdx);
        const value = line.slice(colonIdx + 1).replace(/^ /, ""); // optional leading space

        if (field === "event") {
          eventType = value;
        } else if (field === "data") {
          dataBuffer += (dataBuffer ? "\n" : "") + value;
        }
      }

      if (eventType && dataBuffer) {
        // Forward compat: ignore unknown events
        if (["meta", "delta", "done", "error"].includes(eventType)) {
          try {
            const data = JSON.parse(dataBuffer);
            events.push({ type: eventType, ...data } as ChatStreamEvent);
          } catch (e) {
            console.warn("Failed to parse SSE data JSON", e);
            // Skip invalid JSON frame
          }
        }
      }
    }

    return events;
  };
}

import { getIdToken, clearSession } from "./auth";
import { chatUrl } from "./api";

export async function streamChat(
  bookId: string,
  body: AskRequest,
  handlers: {
    onMeta: (meta: Extract<ChatStreamEvent, { type: "meta" }>) => void;
    onDelta: (text: string) => void;
    onDone: (finishReason: FinishReason, usage?: ChatUsage) => void;
    onError: (code: string, message: string) => void;
  },
  signal: AbortSignal,
): Promise<void> {
  const token = await getIdToken();
  
  try {
    const res = await fetch(chatUrl(`/books/${bookId}/chat`), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { "Authorization": `Bearer ${token}` } : {})
      },
      body: JSON.stringify(body),
      signal
    });

    if (!res.ok) {
      if (res.status === 401) {
        clearSession();
        window.location.href = "/login";
        throw new Error("UNAUTHENTICATED");
      }
      
      const errBody = await res.json().catch(() => ({}));
      const code = errBody.code || "UNKNOWN_ERROR";
      const message = errBody.message || "Failed to start chat stream";
      const error = new Error(message) as any;
      error.code = code;
      if (res.status === 429 && errBody.retryAfterSeconds) {
        error.retryAfterSeconds = errBody.retryAfterSeconds;
      }
      throw error;
    }

    if (!res.body) {
      throw new Error("Response body is null");
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    const parse = createSseParser();
    let receivedDone = false;

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const events = parse(chunk);

        for (const event of events) {
          if (event.type === "meta") handlers.onMeta(event);
          else if (event.type === "delta") handlers.onDelta(event.text);
          else if (event.type === "error") handlers.onError(event.code, event.message);
          else if (event.type === "done") {
            receivedDone = true;
            handlers.onDone(event.finishReason, event.usage);
          }
        }
      }
    } finally {
      reader.releaseLock();
    }

    // Flush decoder
    const finalChunk = decoder.decode();
    if (finalChunk) {
      const finalEvents = parse(finalChunk);
      for (const event of finalEvents) {
        if (event.type === "meta") handlers.onMeta(event);
        else if (event.type === "delta") handlers.onDelta(event.text);
        else if (event.type === "error") handlers.onError(event.code, event.message);
        else if (event.type === "done") {
          receivedDone = true;
          handlers.onDone(event.finishReason, event.usage);
        }
      }
    }

    // AbortSignal aborts the reader and does not call onDone.
    // However, if the reader finishes normally but no 'done' event was received:
    if (!signal.aborted && !receivedDone) {
      handlers.onDone("TRUNCATED");
    }
  } catch (err: any) {
    if (err.name === "AbortError") {
      // Aborted, don't call anything
      return;
    }
    throw err;
  }
}
