// RN port of frontend/hooks/useChat.ts. Same streaming/optimistic-message
// contract; `requestAnimationFrame` is available as an RN global (used here
// exactly as on web, to batch delta text into ~60Hz renders instead of one
// render per SSE frame).

import { useCallback, useEffect, useRef, useState } from "react";
import { streamChat, type AskRequest } from "@/lib/chat";
import { type ChatMessage, type ChatEnvelope, listChat } from "@/lib/books";

export function useChat(bookId: string, anchorRef: React.MutableRefObject<number>) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chat, setChat] = useState<ChatEnvelope | null>(null);
  const [loading, setLoading] = useState(true);
  const [streamingText, setStreamingText] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingError, setStreamingError] = useState<{ code: string; message: string } | null>(
    null,
  );
  const [streamingFinishReason, setStreamingFinishReason] = useState<string | null>(null);
  const [streamingAnchorChunk, setStreamingAnchorChunk] = useState<number | null>(null);

  const bufferRef = useRef("");
  const frameRef = useRef<number | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let cancelled = false;
    listChat(bookId)
      .then((data) => {
        if (!cancelled) {
          setMessages(data.messages);
          setChat(data.chat);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          console.error(err);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [bookId]);

  const sendQuestion = useCallback(
    async (question: string, positionMs: number | null = null) => {
      if (!question.trim()) return;
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }

      const tempId = `temp-${Date.now()}`;
      const anchorChunk = anchorRef.current;

      const userMsg: ChatMessage = {
        id: tempId,
        role: "user",
        content: question,
        anchoredChunk: anchorChunk,
        createdAt: new Date().toISOString(),
        positionMs,
        model: null,
        finishReason: null,
        inputTokens: null,
        outputTokens: null,
        cachedInputTokens: null,
      };

      setMessages((prev) => [...prev, userMsg]);
      setIsStreaming(true);
      setStreamingText("");
      setStreamingError(null);
      setStreamingFinishReason(null);
      setStreamingAnchorChunk(anchorChunk);

      bufferRef.current = "";

      const body: AskRequest = { question, anchorChunk, positionMs };

      // Set once any byte of the response has arrived, so a retry never
      // fires after the user has already seen part of an answer.
      let receivedAnyBytes = false;

      const attempt = () => {
        const abortController = new AbortController();
        abortControllerRef.current = abortController;
        return streamChat(
          bookId,
          body,
          {
            onMeta: (meta) => {
              receivedAnyBytes = true;
              setStreamingAnchorChunk(meta.anchorChunk);
              setChat((prev) =>
                prev ? { ...prev, enabled: meta.enabled, reason: meta.reason, model: meta.model } : prev,
              );
            },
            onDelta: (text) => {
              receivedAnyBytes = true;
              bufferRef.current += text;
              if (frameRef.current === null) {
                frameRef.current = requestAnimationFrame(() => {
                  frameRef.current = null;
                  setStreamingText(bufferRef.current);
                });
              }
            },
            onDone: (finishReason) => {
              setStreamingFinishReason(finishReason);
              // isStreaming stays true until the refetched messages are
              // actually in hand: flipping it false here would unmount the
              // streaming bubble a render before the real completed message
              // exists, producing a visible flash of nothing in between.
              listChat(bookId)
                .then((data) => {
                  setMessages(data.messages);
                  setChat(data.chat);
                  setIsStreaming(false);
                })
                .catch((err) => {
                  console.error(err);
                  setIsStreaming(false);
                });
            },
            onError: (code, message) => {
              setStreamingError({ code, message });
            },
          },
          abortController.signal,
        );
      };

      try {
        await attempt();
      } catch (err: any) {
        if (err.name === "AbortError") return;

        if (!receivedAnyBytes) {
          // One silent retry, mirroring the backend's own retry-once policy:
          // safe because nothing has been shown on screen yet.
          try {
            await attempt();
            return;
          } catch (retryErr: any) {
            if (retryErr.name === "AbortError") return;
            console.error("streamChat failed after retry", retryErr);
            setIsStreaming(false);
            setStreamingError({ code: retryErr.code || "ERR", message: retryErr.message });
            return;
          }
        }

        console.error("streamChat failed", err);
        setIsStreaming(false);
        setStreamingError({ code: err.code || "ERR", message: err.message });
      }
    },
    [bookId, anchorRef],
  );

  useEffect(() => {
    return () => {
      if (frameRef.current !== null) {
        cancelAnimationFrame(frameRef.current);
      }
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, []);

  return {
    messages,
    chat,
    loading,
    isStreaming,
    streamingText,
    streamingError,
    streamingFinishReason,
    streamingAnchorChunk,
    sendQuestion,
  };
}
