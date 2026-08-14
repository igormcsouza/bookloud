import React, { useEffect, useRef } from "react";
import { useChat } from "@/hooks/useChat";
import ChatComposer from "./ChatComposer";
import ChatTurn from "./ChatTurn";

interface ChatSidebarProps {
  bookId: string;
  anchorRef: React.MutableRefObject<number>;
  onSeekToChunk?: (chunkIndex: number) => void;
  chunksTotal: number;
}

export default function ChatSidebar({ bookId, anchorRef, onSeekToChunk, chunksTotal }: ChatSidebarProps) {
  const {
    messages,
    chat,
    loading,
    isStreaming,
    streamingText,
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

  if (loading) {
    return <div className="flex w-80 flex-col border-l border-gray-200 bg-white shadow-sm p-4 text-sm text-gray-500">Loading chat...</div>;
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
    <div className="flex w-80 flex-col border-l border-gray-200 bg-white shadow-sm">
      <div className="flex-1 overflow-y-auto p-4" ref={scrollRef}>
        {notice && (
          <div className="mb-6 rounded-md bg-blue-50 p-3 text-sm text-blue-700">
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
      </div>
      
      <ChatComposer 
        onSend={sendQuestion} 
        disabled={composerDisabled || isStreaming} 
        placeholder={placeholder}
      />
    </div>
  );
}
