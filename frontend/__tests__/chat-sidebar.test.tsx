import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChatSidebar from "@/components/ChatSidebar";
import ChatTurn from "@/components/ChatTurn";
import type { ChatMessage, ChatEnvelope } from "@/lib/books";

const sendQuestionMock = vi.fn();

let mockReturn: {
  messages: ChatMessage[];
  chat: ChatEnvelope | null;
  loading: boolean;
  isStreaming: boolean;
  streamingText: string;
  streamingError: { code: string; message: string } | null;
  streamingFinishReason: string | null;
  streamingAnchorChunk: number | null;
  sendQuestion: typeof sendQuestionMock;
};

vi.mock("@/hooks/useChat", () => ({
  useChat: () => mockReturn,
}));

function msg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "1",
    role: "assistant",
    content: "An answer.",
    anchoredChunk: 12,
    createdAt: "t",
    positionMs: null,
    model: "stub",
    finishReason: null,
    inputTokens: null,
    outputTokens: null,
    cachedInputTokens: null,
    ...overrides,
  };
}

function baseReturn(overrides: Partial<typeof mockReturn> = {}): typeof mockReturn {
  return {
    messages: [],
    chat: { enabled: true, reason: null, model: "stub", dailyLimit: 50, usedToday: 0 },
    loading: false,
    isStreaming: false,
    streamingText: "",
    streamingError: null,
    streamingFinishReason: null,
    streamingAnchorChunk: null,
    sendQuestion: sendQuestionMock,
    ...overrides,
  };
}

afterEach(cleanup);
beforeEach(() => {
  sendQuestionMock.mockReset();
});

describe("ChatSidebar notices (PLANS/phase-7.md §8.4)", () => {
  it("NOT_CONFIGURED: exact copy, composer disabled", () => {
    mockReturn = baseReturn({
      chat: { enabled: false, reason: "NOT_CONFIGURED", model: null, dailyLimit: 50, usedToday: 0 },
    });
    render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onClose={vi.fn()} />);

    expect(
      screen.getByText("Chat isn't available on this deployment yet — no language-model key is configured."),
    ).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Chat not available")).toBeDisabled();
  });

  it("NON_PROD: different copy from NOT_CONFIGURED, composer stays enabled", () => {
    mockReturn = baseReturn({
      chat: { enabled: false, reason: "NON_PROD", model: null, dailyLimit: 50, usedToday: 0 },
    });
    render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onClose={vi.fn()} />);

    expect(
      screen.getByText("Chat is turned off in this environment. Answers are placeholders."),
    ).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Ask a question...")).toBeEnabled();
  });

  it("daily limit reached: composer disabled", () => {
    mockReturn = baseReturn({
      chat: { enabled: true, reason: null, model: "stub", dailyLimit: 50, usedToday: 50 },
    });
    render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onClose={vi.fn()} />);

    expect(
      screen.getByText("You've reached today's question limit (50). It resets at midnight UTC."),
    ).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Daily limit reached")).toBeDisabled();
  });

  it("no text yet (chunksTotal === 0): composer disabled", () => {
    mockReturn = baseReturn();
    render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={0} onClose={vi.fn()} />);

    expect(
      screen.getByText("Chat becomes available once the text is extracted."),
    ).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Waiting for text...")).toBeDisabled();
  });

  it("a completed turn with finishReason ERROR renders the inline chip", () => {
    mockReturn = baseReturn({ messages: [msg({ finishReason: "ERROR" })] });
    render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onClose={vi.fn()} />);
    expect(screen.getByText("The answer stopped early. Ask again.")).toBeInTheDocument();
  });

  it("a completed turn with finishReason TRUNCATED renders the inline chip", () => {
    mockReturn = baseReturn({ messages: [msg({ finishReason: "TRUNCATED" })] });
    render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onClose={vi.fn()} />);
    expect(screen.getByText("Response interrupted.")).toBeInTheDocument();
  });

  it("a completed turn with finishReason MAX_TOKENS renders the inline chip", () => {
    mockReturn = baseReturn({ messages: [msg({ finishReason: "MAX_TOKENS" })] });
    render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onClose={vi.fn()} />);
    expect(screen.getByText("Answer cut off — ask a narrower question.")).toBeInTheDocument();
  });

  it("a pre-stream network failure renders a visible error instead of silently vanishing", () => {
    // Regression: useChat returned streamingError but ChatSidebar never read
    // it, so a failed request (e.g. the Lambda cold-start 503) just made the
    // streaming bubble disappear with no indication anything went wrong.
    mockReturn = baseReturn({
      isStreaming: false,
      streamingError: { code: "ERR", message: "Failed to fetch" },
    });
    render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onClose={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't get an answer");
    expect(screen.getByRole("alert")).toHaveTextContent("Failed to fetch");
  });

  it("does not render the error banner while still streaming", () => {
    mockReturn = baseReturn({
      isStreaming: true,
      streamingError: { code: "ERR", message: "Failed to fetch" },
    });
    render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onClose={vi.fn()} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("ChatSidebar close button", () => {
  it("calls onClose when the header close button is clicked", () => {
    const onClose = vi.fn();
    mockReturn = baseReturn();
    render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onClose={onClose} />);

    fireEvent.click(screen.getByRole("button", { name: "Close chat" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("ChatSidebar anchor label", () => {
  it("renders 'about section 12' and calls onSeekToChunk when clicked", () => {
    const onSeek = vi.fn();
    mockReturn = baseReturn({ messages: [msg({ anchoredChunk: 12 })] });
    render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onSeekToChunk={onSeek} onClose={vi.fn()} />);

    fireEvent.click(screen.getByText("about section 12"));
    expect(onSeek).toHaveBeenCalledWith(12);
  });
});

describe("ChatSidebar / ChatTurn memoization", () => {
  it("a completed ChatTurn does not re-render while a new turn streams", () => {
    // React.memo skips calling the wrapped function entirely when props are
    // shallow-equal -- the signal that matters is whether ChatTurn's inner
    // render function runs again, not whether the parent evaluates a prop
    // expression (it always does, to build the JSX element). Patching
    // `.type` (the inner function `React.memo` wraps) counts real renders of
    // the real production component, phase-6's ChunkParagraph pattern
    // adapted for a component the parent addresses by scalar props rather
    // than one aggregate object.
    const inner = (ChatTurn as unknown as { type: (...args: unknown[]) => unknown }).type;
    let renderCount = 0;
    (ChatTurn as unknown as { type: (...args: unknown[]) => unknown }).type = (...args: unknown[]) => {
      renderCount += 1;
      return inner(...args);
    };

    try {
      mockReturn = baseReturn({
        messages: [msg({ id: "done-1" })],
        isStreaming: true,
        streamingText: "partial",
      });
      const { rerender } = render(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onClose={vi.fn()} />);
      const rendersAfterFirst = renderCount; // completed turn + streaming turn

      mockReturn = { ...mockReturn, streamingText: "partial answer growing" };
      rerender(<ChatSidebar bookId="b1" anchorRef={{ current: 0 }} chunksTotal={3} onClose={vi.fn()} />);

      // Only the streaming turn's content changed, so exactly one more
      // ChatTurn render -- the completed one bailed out.
      expect(renderCount).toBe(rendersAfterFirst + 1);
    } finally {
      (ChatTurn as unknown as { type: (...args: unknown[]) => unknown }).type = inner;
    }
  });
});
