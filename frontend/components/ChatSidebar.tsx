import React, { useEffect, useRef } from "react";
import { useChat } from "@/hooks/useChat";
import ChatComposer from "./ChatComposer";
import ChatTurn from "./ChatTurn";

interface ChatSidebarProps {
  bookId: string;
  anchorRef: React.MutableRefObject<number>;
  onSeekToChunk?: (chunkIndex: number) => void;
  chunksTotal: number;
  onClose: () => void;
}

export default function ChatSidebar({ bookId, anchorRef, onSeekToChunk, chunksTotal, onClose }: ChatSidebarProps) {
  const {
    messages,
    chat,
    loading,
    isStreaming,
    streamingText,
    streamingError,
    streamingFinishReason,
    streamingAnchorChunk,
    sendQuestion
  } = useChat(bookId, anchorRef);

  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, streamingText]);

  const header = (
    <div className="flex items-center justify-between border-b border-ink-800 px-4 py-3">
      <span className="text-sm font-medium text-paper">Chat</span>
      <button
        type="button"
        onClick={onClose}
        aria-label="Close chat"
        title="Close chat (Alt+C)"
        className="rounded p-1 text-sage hover:bg-ink-800 hover:text-paper"
      >
        <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  );

  if (loading) {
    return (
      <div className="flex w-80 flex-col border-l border-ink-800 bg-ink-950">
        {header}
        <div className="p-4 text-sm text-sage">Loading chat...</div>
      </div>
    );
  }

  let notice = null;
  let composerDisabled = false;
  let placeholder = "Ask a question...";

  if (chunksTotal === 0) {
    notice = "Chat becomes available once the text is extracted.";
    composerDisabled = true;
    placeholder = "Waiting for text...";
  } else if (chat?.enabled === false && chat?.reason === "NOT_CONFIGURED") {
    notice = "Chat isn't available on this deployment yet — no language-model key is configured.";
    composerDisabled = true;
    placeholder = "Chat not available";
  } else if (chat?.enabled === false && chat?.reason === "NON_PROD") {
    notice = "Chat is turned off in this environment. Answers are placeholders.";
    // composer is enabled
  } else if (chat?.usedToday !== undefined && chat?.dailyLimit !== undefined && chat.usedToday >= chat.dailyLimit) {
    notice = `You've reached today's question limit (${chat.dailyLimit}). It resets at midnight UTC.`;
    composerDisabled = true;
    placeholder = "Daily limit reached";
  }

  return (
    <div className="flex w-80 flex-col border-l border-ink-800 bg-ink-950">
      {header}
      <div className="flex-1 overflow-y-auto p-4" ref={scrollRef}>
        {notice && (
          <div
            className={`mb-6 rounded-lg border px-4 py-3 text-sm ${
              composerDisabled ? "border-moss-500/40 bg-ink-900 text-paper" : "border-ink-800 bg-ink-900 text-sage"
            }`}
          >
            {notice}
          </div>
        )}
        
        {messages.map((m) => (
          <ChatTurn 
            key={m.id}
            id={m.id}
            role={m.role}
            content={m.content}
            finishReason={m.finishReason}
            anchoredChunk={m.role === "assistant" ? m.anchoredChunk : null}
            onSeek={onSeekToChunk}
          />
        ))}

        {isStreaming && (
          <ChatTurn
            id="streaming"
            role="assistant"
            content={streamingText}
            finishReason={streamingFinishReason}
            anchoredChunk={streamingAnchorChunk}
            isStreaming={true}
            onSeek={onSeekToChunk}
          />
        )}

        {!isStreaming && streamingError && (
          // A request that failed before producing any persisted turn --
          // most often the Lambda's cold-start window (AWS_LWA_ASYNC_INIT
          // used to drop the first request on a cold container with a 503
          // even though it succeeded server-side a moment later). Without
          // this the streaming bubble above just vanishes with zero
          // indication anything went wrong.
          <div
            role="alert"
            className="mb-4 rounded-lg border border-rose-500/40 bg-ink-900 px-4 py-3 text-sm text-rose-200"
          >
            Couldn't get an answer ({streamingError.message || "network error"}). Try asking again.
          </div>
        )}
      </div>
      
      <ChatComposer 
        onSend={sendQuestion} 
        disabled={composerDisabled || isStreaming} 
        placeholder={placeholder}
      />
    </div>
  );
}
