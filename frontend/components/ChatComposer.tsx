import React, { useState } from "react";

interface ChatComposerProps {
  onSend: (text: string) => void;
  disabled: boolean;
  placeholder?: string;
}

export default function ChatComposer({ onSend, disabled, placeholder = "Ask a question..." }: ChatComposerProps) {
  const [text, setText] = useState("");

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!disabled && text.trim().length > 0 && text.length <= 2000) {
        onSend(text);
        setText("");
      }
    }
  };

  const handleSend = () => {
    if (!disabled && text.trim().length > 0 && text.length <= 2000) {
      onSend(text);
      setText("");
    }
  };

  return (
    <div className="border-t border-ink-800 p-4">
      <div className="relative">
        <textarea
          className="w-full resize-none rounded-md border border-ink-800 bg-ink-900 p-3 pr-12 text-paper placeholder:text-sage focus:border-moss-400 focus:outline-none focus:ring-1 focus:ring-moss-400 disabled:opacity-40 sm:text-sm"
          rows={3}
          placeholder={placeholder}
          value={text}
          onChange={(e) => setText(e.target.value.slice(0, 2000))}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          maxLength={2000}
        />
        <button
          onClick={handleSend}
          disabled={disabled || text.trim().length === 0}
          className="absolute bottom-3 right-3 rounded bg-moss-400 p-1.5 text-ink-950 hover:bg-moss-300 disabled:cursor-not-allowed disabled:bg-ink-800 disabled:text-sage disabled:opacity-40"
          aria-label="Send"
        >
          <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
             <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 12h14M12 5l7 7-7 7" />
          </svg>
        </button>
      </div>
    </div>
  );
}
