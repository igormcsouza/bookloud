import { useCallback, useEffect, useRef, useState } from "react";
import { streamChat, type ChatStreamEvent, type AskRequest } from "@/lib/chat";
import { type ChatMessage, type ChatEnvelope, listChat } from "@/lib/books";

export function useChat(bookId: string, anchorRef: React.MutableRefObject<number>) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chat, setChat] = useState<ChatEnvelope | null>(null);
  const [loading, setLoading] = useState(true);
  const [streamingText, setStreamingText] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingError, setStreamingError] = useState<{ code: string; message: string } | null>(null);
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
    async (question: string) => {
      if (!question.trim()) return;
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
      
      const abortController = new AbortController();
      abortControllerRef.current = abortController;

      // Optimistically add user message
      const tempId = `temp-${Date.now()}`;
      const anchorChunk = anchorRef.current;
      
      const userMsg: ChatMessage = {
        id: tempId,
        role: "user",
        content: question,
        anchoredChunk: anchorChunk,
        createdAt: new Date().toISOString(),
        positionMs: null, // we don't have playback easily here unless we pass it, but spec says "where playback was". Actually positionMs is from AskRequest. Wait, AskRequest takes positionMs. We can pass it or just null for now since we just use anchorChunk.
        model: null,
        finishReason: null,
        inputTokens: null,
        outputTokens: null,
        cachedInputTokens: null
      };

      setMessages((prev) => [...prev, userMsg]);
      setIsStreaming(true);
      setStreamingText("");
      setStreamingError(null);
      setStreamingFinishReason(null);
      setStreamingAnchorChunk(anchorChunk); // updated by meta if different
      
      bufferRef.current = "";

      const body: AskRequest = {
        question,
        anchorChunk,
        positionMs: null // TODO if needed
      };

      try {
        await streamChat(
          bookId,
          body,
          {
            onMeta: (meta) => {
              setStreamingAnchorChunk(meta.anchorChunk);
              setChat((prev) => {
                if (!prev) return prev;
                return {
                  ...prev,
                  enabled: meta.enabled,
                  reason: meta.reason,
                  model: meta.model
                };
              });
            },
            onDelta: (text) => {
              bufferRef.current += text;
              if (frameRef.current === null) {
                frameRef.current = requestAnimationFrame(() => {
                  frameRef.current = null;
                  setStreamingText(bufferRef.current);
                });
              }
            },
            onDone: (finishReason) => {
              // We don't append to messages here, we just set finish reason and let a refetch happen or we can construct the message.
              // Actually, the simplest is to fetch listChat again after done.
              setStreamingFinishReason(finishReason);
              setIsStreaming(false);
              listChat(bookId).then(data => {
                setMessages(data.messages);
                setChat(data.chat);
              }).catch(console.error);
            },
            onError: (code, message) => {
              setStreamingError({ code, message });
            }
          },
          abortController.signal
        );
      } catch (err: any) {
        if (err.name !== "AbortError") {
          console.error("streamChat failed", err);
          setIsStreaming(false);
          setStreamingError({ code: err.code || "ERR", message: err.message });
        }
      }
    },
    [bookId, anchorRef]
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
    sendQuestion
  };
}
