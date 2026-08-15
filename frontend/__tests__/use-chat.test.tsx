import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChat } from "@/hooks/useChat";
import type { ChatEnvelope } from "@/lib/books";

const streamChatMock = vi.fn();
const listChatMock = vi.fn();

vi.mock("@/lib/chat", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/chat")>();
  return { ...actual, streamChat: (...args: unknown[]) => streamChatMock(...args) };
});

vi.mock("@/lib/books", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/books")>();
  return { ...actual, listChat: (...args: unknown[]) => listChatMock(...args) };
});

const ENABLED_ENVELOPE: ChatEnvelope = {
  enabled: true,
  reason: null,
  model: "stub",
  dailyLimit: 50,
  usedToday: 0,
};

type Handlers = {
  onMeta: (meta: { model: string | null; enabled: boolean; reason: string | null; anchorChunk: number; windowChunks: number[]; messageId: string }) => void;
  onDelta: (text: string) => void;
  onDone: (finishReason: string) => void;
  onError: (code: string, message: string) => void;
};

let frames: FrameRequestCallback[] = [];

function installRafStub() {
  frames = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    frames.push(cb);
    return frames.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {
    frames = [];
  });
}

async function tickFrame() {
  const next = frames.shift();
  if (!next) return;
  await act(async () => {
    next(performance.now());
  });
}

function Host({ chatRef, onRender }: { chatRef: { current: ReturnType<typeof useChat> | null }; onRender: () => void }) {
  onRender();
  const chat = useChat("b1", { current: 0 });
  chatRef.current = chat;
  return <div data-testid="streaming-text">{chat.streamingText}</div>;
}

describe("useChat", () => {
  beforeEach(() => {
    streamChatMock.mockReset();
    listChatMock.mockReset();
    listChatMock.mockResolvedValue({ messages: [], chat: ENABLED_ENVELOPE });
    installRafStub();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    cleanup();
  });

  it("shows the optimistic user turn immediately on send", async () => {
    let capturedHandlers: Handlers | null = null;
    streamChatMock.mockImplementation((_id, _body, handlers) => {
      capturedHandlers = handlers;
      return new Promise(() => {}); // never resolves for this test
    });

    const chatRef: { current: ReturnType<typeof useChat> | null } = { current: null };
    render(<Host chatRef={chatRef} onRender={() => {}} />);
    await waitFor(() => expect(chatRef.current!.loading).toBe(false));

    act(() => {
      chatRef.current!.sendQuestion("What happens next?");
    });

    expect(chatRef.current!.messages).toHaveLength(1);
    expect(chatRef.current!.messages[0]).toMatchObject({ role: "user", content: "What happens next?" });
    expect(capturedHandlers).not.toBeNull();
  });

  it("composer is disabled (isStreaming true) while a stream is open, and re-enabled on done", async () => {
    let capturedHandlers: Handlers | null = null;
    streamChatMock.mockImplementation((_id, _body, handlers) => {
      capturedHandlers = handlers;
      return Promise.resolve();
    });

    const chatRef: { current: ReturnType<typeof useChat> | null } = { current: null };
    render(<Host chatRef={chatRef} onRender={() => {}} />);
    await waitFor(() => expect(chatRef.current!.loading).toBe(false));

    act(() => {
      chatRef.current!.sendQuestion("Q1");
    });
    expect(chatRef.current!.isStreaming).toBe(true);

    listChatMock.mockResolvedValue({
      messages: [
        { id: "1", role: "user", content: "Q1", anchoredChunk: 0, createdAt: "t1", positionMs: null, model: null, finishReason: null, inputTokens: null, outputTokens: null, cachedInputTokens: null },
        { id: "2", role: "assistant", content: "A1", anchoredChunk: 0, createdAt: "t2", positionMs: null, model: "stub", finishReason: "DISABLED", inputTokens: null, outputTokens: null, cachedInputTokens: null },
      ],
      chat: ENABLED_ENVELOPE,
    });

    await act(async () => {
      capturedHandlers!.onDone("DISABLED");
    });

    await waitFor(() => expect(chatRef.current!.isStreaming).toBe(false));
  });

  it("accumulates deltas into one assistant turn, flushing at most 3 renders over 60 deltas across 2 rAF frames", async () => {
    let capturedHandlers: Handlers | null = null;
    streamChatMock.mockImplementation((_id, _body, handlers) => {
      capturedHandlers = handlers;
      return new Promise(() => {});
    });

    const chatRef: { current: ReturnType<typeof useChat> | null } = { current: null };
    let renders = 0;
    render(<Host chatRef={chatRef} onRender={() => (renders += 1)} />);
    await waitFor(() => expect(chatRef.current!.loading).toBe(false));

    const rendersBeforeSend = renders;

    act(() => {
      chatRef.current!.sendQuestion("Q1");
    });
    expect(capturedHandlers).not.toBeNull();

    act(() => {
      for (let i = 0; i < 30; i += 1) capturedHandlers!.onDelta("x");
    });
    await tickFrame();

    act(() => {
      for (let i = 0; i < 30; i += 1) capturedHandlers!.onDelta("x");
    });
    await tickFrame();

    expect(chatRef.current!.streamingText).toBe("x".repeat(60));
    expect(renders - rendersBeforeSend).toBeLessThanOrEqual(3);
  });

  it("a second question appends to the transcript rather than replacing it", async () => {
    let capturedHandlers: Handlers | null = null;
    streamChatMock.mockImplementation((_id, _body, handlers) => {
      capturedHandlers = handlers;
      return Promise.resolve();
    });

    const chatRef: { current: ReturnType<typeof useChat> | null } = { current: null };
    render(<Host chatRef={chatRef} onRender={() => {}} />);
    await waitFor(() => expect(chatRef.current!.loading).toBe(false));

    listChatMock.mockResolvedValue({
      messages: [
        { id: "1", role: "user", content: "Q1", anchoredChunk: 0, createdAt: "t1", positionMs: null, model: null, finishReason: null, inputTokens: null, outputTokens: null, cachedInputTokens: null },
        { id: "2", role: "assistant", content: "A1", anchoredChunk: 0, createdAt: "t2", positionMs: null, model: "stub", finishReason: "DISABLED", inputTokens: null, outputTokens: null, cachedInputTokens: null },
      ],
      chat: ENABLED_ENVELOPE,
    });

    act(() => {
      chatRef.current!.sendQuestion("Q1");
    });
    await act(async () => {
      capturedHandlers!.onDone("DISABLED");
    });
    await waitFor(() => expect(chatRef.current!.messages).toHaveLength(2));

    act(() => {
      chatRef.current!.sendQuestion("Q2");
    });

    expect(chatRef.current!.messages).toHaveLength(3);
    expect(chatRef.current!.messages.map((m) => m.content)).toEqual(["Q1", "A1", "Q2"]);
  });

  it("aborts the in-flight stream on unmount", async () => {
    let capturedSignal: AbortSignal | null = null;
    streamChatMock.mockImplementation((_id, _body, _handlers, signal: AbortSignal) => {
      capturedSignal = signal;
      return new Promise(() => {});
    });

    const chatRef: { current: ReturnType<typeof useChat> | null } = { current: null };
    const { unmount } = render(<Host chatRef={chatRef} onRender={() => {}} />);
    await waitFor(() => expect(chatRef.current!.loading).toBe(false));

    act(() => {
      chatRef.current!.sendQuestion("Q1");
    });
    expect(capturedSignal).not.toBeNull();
    expect(capturedSignal!.aborted).toBe(false);

    unmount();

    expect(capturedSignal!.aborted).toBe(true);
  });
});
