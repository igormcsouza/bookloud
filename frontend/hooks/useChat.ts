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
              // We don't append to messages here, we just set finish reason and let a refetch happen or we can construct the message.
              // Actually, the simplest is to fetch listChat again after done.
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
            }
          },
          abortController.signal
        );
      };

      try {
        await attempt();
      } catch (err: any) {
        if (err.name === "AbortError") return;

        if (!receivedAnyBytes) {
          // A request that failed before a single byte arrived -- observed
          // in practice as the Function URL's streamed response
          // occasionally resetting the connection even though the Lambda
          // invocation itself completed successfully server-side (nothing
          // an app-level fix can reach; PLANS/phase-7.md §12.3 flags
          // response-streaming reliability as unverifiable by any
          // automated check). One silent retry, mirroring the backend's own
          // retry-once-before-first-token policy for OpenAI: safe because
          // nothing has been shown on screen yet, so a retry can never
          // duplicate visible text.
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
