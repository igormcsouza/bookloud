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
  // Bumped on every sendQuestion call; a background reconcile fetch checks
  // this before applying its result so a slow/stale response from an older
  // question can never clobber a newer answer that already landed.
  const generationRef = useRef(0);
  const metaModelRef = useRef<string | null>(null);

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
      const generation = ++generationRef.current;

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
              metaModelRef.current = meta.model;
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
            onDone: (finishReason, usage) => {
              setStreamingFinishReason(finishReason);
              // The completed answer becomes part of `messages` immediately,
              // from data we already have locally -- it must never depend on
              // a round trip back to the server to stay on screen. A stray
              // eventually-consistent read of a just-written DynamoDB item
              // (or a slow response) used to make the just-streamed answer
              // vanish for anyone still looking at it.
              const assistantMsg: ChatMessage = {
                id: `temp-assistant-${Date.now()}`,
                role: "assistant",
                content: bufferRef.current,
                anchoredChunk: anchorChunk,
                createdAt: new Date().toISOString(),
                positionMs,
                model: metaModelRef.current,
                finishReason,
                inputTokens: usage?.inputTokens ?? null,
                outputTokens: usage?.outputTokens ?? null,
                cachedInputTokens: usage?.cachedInputTokens ?? null,
              };
              setMessages((prev) => [...prev, assistantMsg]);
              setStreamingText("");
              setIsStreaming(false);

              // Fire-and-forget reconcile with the server's canonical copy
              // (real ids/timestamps/usage). Only applied if it's still the
              // latest question and it actually contains an answer to it --
              // never used to remove what's already showing.
              listChat(bookId)
                .then((data) => {
                  if (generationRef.current !== generation) return;
                  const hasAssistantReply = data.messages.some(
                    (m) => m.role === "assistant" && m.content === bufferRef.current,
                  );
                  if (!hasAssistantReply) return;
                  setMessages(data.messages);
                  setChat(data.chat);
                })
                .catch((err) => {
                  console.error(err);
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
