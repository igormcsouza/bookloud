import React from "react";

interface ChatTurnProps {
  id: string;
  role: "user" | "assistant";
  content: string;
  finishReason: string | null;
  anchoredChunk?: number | null;
  onSeek?: (chunkIndex: number) => void;
  isStreaming?: boolean;
}

const ChatTurn = React.memo(function ChatTurn({ id, role, content, finishReason, anchoredChunk, onSeek, isStreaming }: ChatTurnProps) {
  const isUser = role === "user";
  
  // Render text with paragraph breaks
  const renderContent = (text: string) => {
    return text.split("\n").map((line, i) => (
      <React.Fragment key={i}>
        {line}
        {i < text.split("\n").length - 1 && <br />}
      </React.Fragment>
    ));
  };

  return (
    <div className={`mb-4 flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[85%] rounded-lg px-4 py-2 ${isUser ? "bg-moss-500 text-ink-950" : "bg-ink-800 text-paper"}`}>
        <div className="text-sm">
          {isStreaming && content === "" ? (
            <span className="inline-flex items-center gap-1 py-1" role="status" aria-label="Thinking">
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-sage [animation-delay:-0.3s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-sage [animation-delay:-0.15s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-sage" />
            </span>
          ) : (
            <>
              {renderContent(content)}
              {isStreaming && <span className="ml-1 animate-pulse">▋</span>}
            </>
          )}
        </div>

        {!isUser && anchoredChunk != null && (
          <div className="mt-2 text-xs">
            <button
              onClick={() => onSeek?.(anchoredChunk)}
              className="text-moss-300 hover:underline"
            >
              about section {anchoredChunk}
            </button>
          </div>
        )}

        {finishReason === "ERROR" && (
          <div className="mt-2 inline-flex items-center rounded-full bg-rose-500/20 px-2.5 py-0.5 text-xs font-medium text-rose-200">
            The answer stopped early. Ask again.
          </div>
        )}
        {finishReason === "TRUNCATED" && (
          <div className="mt-2 inline-flex items-center rounded-full bg-moss-500/15 px-2.5 py-0.5 text-xs font-medium text-moss-200">
            Response interrupted.
          </div>
        )}
        {finishReason === "MAX_TOKENS" && (
          <div className="mt-2 inline-flex items-center rounded-full bg-moss-500/15 px-2.5 py-0.5 text-xs font-medium text-moss-200">
            Answer cut off — ask a narrower question.
          </div>
        )}
      </div>
    </div>
  );
});

export default ChatTurn;
