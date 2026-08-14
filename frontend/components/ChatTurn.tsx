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
      <div className={`max-w-[85%] rounded-lg px-4 py-2 ${isUser ? "bg-indigo-600 text-white" : "bg-gray-100 text-gray-900"}`}>
        <div className="text-sm">
          {renderContent(content)}
          {isStreaming && <span className="ml-1 animate-pulse">▋</span>}
        </div>
        
        {!isUser && anchoredChunk != null && (
          <div className="mt-2 text-xs">
            <button 
              onClick={() => onSeek?.(anchoredChunk)}
              className="text-indigo-600 hover:underline"
            >
              about section {anchoredChunk}
            </button>
          </div>
        )}

        {finishReason === "ERROR" && (
          <div className="mt-2 inline-flex items-center rounded-full bg-red-100 px-2.5 py-0.5 text-xs font-medium text-red-800">
            The answer stopped early. Ask again.
          </div>
        )}
        {finishReason === "TRUNCATED" && (
          <div className="mt-2 inline-flex items-center rounded-full bg-yellow-100 px-2.5 py-0.5 text-xs font-medium text-yellow-800">
            Response interrupted.
          </div>
        )}
        {finishReason === "MAX_TOKENS" && (
          <div className="mt-2 inline-flex items-center rounded-full bg-yellow-100 px-2.5 py-0.5 text-xs font-medium text-yellow-800">
            Answer cut off — ask a narrower question.
          </div>
        )}
      </div>
    </div>
  );
});

export default ChatTurn;
