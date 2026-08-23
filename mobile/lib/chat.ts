// RN port of frontend/lib/chat.ts. The event types and `AskRequest` shape
// are unchanged -- only the SSE transport differs: RN/Hermes does not
// reliably support `fetch().body.getReader()` across Expo SDK versions, so
// this uses `react-native-sse` (XHR `onprogress`-based) instead of a
// hand-rolled reader loop. The wire format (`meta|delta|error|done` frames)
// is unchanged, so the backend needs no changes either.

import EventSource from "react-native-sse";
import { getIdToken } from "@/lib/auth";
import { notifySessionExpired } from "@/lib/authEvents";
import { chatUrl } from "@/lib/api";

export type ChatDisabledReason = "NOT_CONFIGURED" | "NON_PROD";
export type FinishReason = "END_TURN" | "MAX_TOKENS" | "REFUSAL" | "DISABLED" | "ERROR" | "TRUNCATED";

export interface ChatUsage {
  inputTokens: number;
  outputTokens: number;
  cachedInputTokens: number;
}

export type ChatStreamEvent =
  | {
      type: "meta";
      model: string | null;
      enabled: boolean;
      reason: ChatDisabledReason | null;
      anchorChunk: number;
      windowChunks: number[];
      messageId: string;
    }
  | { type: "delta"; text: string }
  | { type: "done"; finishReason: FinishReason; usage?: ChatUsage; messageId: string }
  | { type: "error"; code: string; message: string };

export interface AskRequest {
  question: string;
  anchorChunk: number | null;
  positionMs: number | null;
}

type EventName = "meta" | "delta" | "done" | "error";
const EVENT_NAMES: EventName[] = ["meta", "delta", "done", "error"];

export class ChatStreamError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly retryAfterSeconds?: number,
  ) {
    super(message);
    this.name = "ChatStreamError";
  }
}

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

  // A single request, not "check with fetch, then open with EventSource":
  // the backend runs the LLM completion and persists a chat turn per POST
  // it receives, so two requests for one visible question would double the
  // cost and could write two history entries. react-native-sse's XHR-based
  // `error` event fires for a non-2xx response WITHOUT ever parsing the body
  // as SSE frames (its onreadystatechange branches on `xhr.status`), and
  // carries the raw response body as `event.message` -- enough to recover
  // the same `{code, message, retryAfterSeconds}` shape the web client reads
  // from a failed preflight, with no second request.
  return new Promise((resolve, reject) => {
    const es = new EventSource<EventName>(chatUrl(`/books/${bookId}/chat`), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
    });

    let receivedDone = false;
    let settled = false;
    const cleanup = () => {
      es.close();
      // react-native-sse's own onreadystatechange handler calls
      // `_pollAgain()` (arming a new reconnect timer) UNCONDITIONALLY right
      // after dispatching our "done" event -- with no check of whether we
      // just closed it. That new timer is armed *after* the `es.close()`
      // above already ran, so it survives that first close() call. A
      // deferred second close() one tick later reliably clears it (its
      // library-default interval is 5000ms, i.e. many ticks away).
      setTimeout(() => es.close(), 0);
    };
    const settle = (fn: () => void) => {
      if (settled) return;
      settled = true;
      cleanup();
      fn();
    };

    signal.addEventListener("abort", () => settle(resolve));

    for (const name of EVENT_NAMES) {
      es.addEventListener(name, (event: any) => {
        if (!event.data) return;
        let data: any;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        if (name === "meta") handlers.onMeta({ type: "meta", ...data });
        else if (name === "delta") handlers.onDelta(data.text);
        else if (name === "error") handlers.onError(data.code, data.message);
        else if (name === "done") {
          receivedDone = true;
          handlers.onDone(data.finishReason, data.usage);
          // Close immediately on our OWN protocol's "done" frame -- do not
          // wait for react-native-sse's transport-level "close"/"error"
          // events. On a clean HTTP completion (200, readyState DONE) the
          // library's onreadystatechange calls `_pollAgain(...)` and
          // RE-ISSUES THE SAME POST with the same body, rather than
          // dispatching a "close" event -- left uncaught, this silently
          // resends the identical question over and over on an interval.
          settle(resolve);
        }
      });
    }

    es.addEventListener("close", () => {
      settle(() => {
        if (!signal.aborted && !receivedDone) handlers.onDone("TRUNCATED");
        resolve();
      });
    });

    es.addEventListener("error", (event: any) => {
      const xhrStatus: number | undefined = event?.xhrStatus;
      if (xhrStatus === 401) {
        notifySessionExpired();
        settle(() => reject(new ChatStreamError("UNAUTHENTICATED", "UNAUTHENTICATED")));
        return;
      }
      if (xhrStatus && xhrStatus >= 400) {
        const errBody = (() => {
          try {
            return JSON.parse(event.message ?? "{}");
          } catch {
            return {};
          }
        })();
        const code = errBody.code || "UNKNOWN_ERROR";
        const message = errBody.message || "Failed to start chat stream";
        settle(() => reject(new ChatStreamError(message, code, errBody.retryAfterSeconds)));
        return;
      }
      settle(() => reject(new Error(event?.message || "Chat stream connection failed")));
    });
  });
}
