import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamChat } from "@/lib/chat";

const getIdTokenMock = vi.fn();
const clearSessionMock = vi.fn();

vi.mock("@/lib/auth", () => ({
  getIdToken: () => getIdTokenMock(),
  clearSession: () => clearSessionMock(),
}));

/** A controllable fake reader: `push` queues an SSE-framed string to be
 * delivered whole on the next `read()`, `end()` completes the stream. Not a
 * real ReadableStream -- this is a unit test of streamChat's frame-handling
 * logic, not of the Streams API. */
function fakeReader(signal: AbortSignal) {
  const queue: string[] = [];
  let ended = false;
  const encoder = new TextEncoder();
  return {
    push(text: string) {
      queue.push(text);
    },
    end() {
      ended = true;
    },
    async read() {
      if (signal.aborted) {
        const err = new Error("aborted");
        err.name = "AbortError";
        throw err;
      }
      if (queue.length > 0) {
        return { done: false, value: encoder.encode(queue.shift()!) };
      }
      if (ended) return { done: true, value: undefined };
      return { done: true, value: undefined };
    },
    releaseLock() {},
  };
}

function fakeStreamResponse(signal: AbortSignal) {
  const reader = fakeReader(signal);
  return {
    reader,
    response: {
      ok: true,
      status: 200,
      body: { getReader: () => reader },
      json: async () => ({}),
    },
  };
}

const NOOP_HANDLERS = {
  onMeta: vi.fn(),
  onDelta: vi.fn(),
  onDone: vi.fn(),
  onError: vi.fn(),
};

function handlers() {
  return {
    onMeta: vi.fn(),
    onDelta: vi.fn(),
    onDone: vi.fn(),
    onError: vi.fn(),
  };
}

describe("streamChat", () => {
  const originalFetch = global.fetch;
  const originalLocation = window.location;

  beforeEach(() => {
    getIdTokenMock.mockReset();
    getIdTokenMock.mockResolvedValue("id-token");
    clearSessionMock.mockReset();
    Object.values(NOOP_HANDLERS).forEach((fn) => fn.mockReset());
  });

  afterEach(() => {
    global.fetch = originalFetch;
    Object.defineProperty(window, "location", { value: originalLocation, writable: true });
  });

  it("a pre-stream 401 rejects with UNAUTHENTICATED and clears the session", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({}),
    }) as unknown as typeof fetch;
    delete (window as any).location;
    (window as any).location = { href: "" };

    const h = handlers();
    await expect(
      streamChat("b1", { question: "q", anchorChunk: 0, positionMs: null }, h, new AbortController().signal),
    ).rejects.toThrow("UNAUTHENTICATED");
    expect(clearSessionMock).toHaveBeenCalled();
  });

  it("a pre-stream 429 rejects with QUOTA_EXCEEDED and surfaces retryAfterSeconds", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 429,
      json: async () => ({ code: "QUOTA_EXCEEDED", message: "slow down", retryAfterSeconds: 3600 }),
    }) as unknown as typeof fetch;

    const h = handlers();
    await expect(
      streamChat("b1", { question: "q", anchorChunk: 0, positionMs: null }, h, new AbortController().signal),
    ).rejects.toMatchObject({ code: "QUOTA_EXCEEDED", retryAfterSeconds: 3600 });
  });

  it("a pre-stream 404 rejects with BOOK_NOT_FOUND", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ code: "BOOK_NOT_FOUND", message: "no such book" }),
    }) as unknown as typeof fetch;

    const h = handlers();
    await expect(
      streamChat("b1", { question: "q", anchorChunk: 0, positionMs: null }, h, new AbortController().signal),
    ).rejects.toMatchObject({ code: "BOOK_NOT_FOUND" });
  });

  it("happy path calls onMeta once, onDelta N times in order, onDone once", async () => {
    const controller = new AbortController();
    const { reader, response } = fakeStreamResponse(controller.signal);
    global.fetch = vi.fn().mockResolvedValue(response) as unknown as typeof fetch;

    reader.push(
      'event: meta\ndata: {"model":"stub","enabled":false,"reason":"NON_PROD","anchorChunk":0,"windowChunks":[0],"messageId":"m1"}\n\n' +
        'event: delta\ndata: {"text":"a"}\n\n' +
        'event: delta\ndata: {"text":"b"}\n\n' +
        'event: done\ndata: {"finishReason":"DISABLED","messageId":"m1"}\n\n',
    );
    reader.end();

    const h = handlers();
    await streamChat("b1", { question: "q", anchorChunk: 0, positionMs: null }, h, controller.signal);

    expect(h.onMeta).toHaveBeenCalledTimes(1);
    expect(h.onDelta.mock.calls.map((c) => c[0])).toEqual(["a", "b"]);
    expect(h.onDone).toHaveBeenCalledTimes(1);
    expect(h.onDone).toHaveBeenCalledWith("DISABLED", undefined);
  });

  it("an error frame mid-stream calls onError then onDone(ERROR), without discarding earlier deltas", async () => {
    const controller = new AbortController();
    const { reader, response } = fakeStreamResponse(controller.signal);
    global.fetch = vi.fn().mockResolvedValue(response) as unknown as typeof fetch;

    reader.push(
      'event: meta\ndata: {"model":"m","enabled":true,"reason":null,"anchorChunk":0,"windowChunks":[0],"messageId":"m1"}\n\n' +
        'event: delta\ndata: {"text":"partial"}\n\n' +
        'event: error\ndata: {"code":"LLM_UNAVAILABLE","message":"stopped early"}\n\n' +
        'event: done\ndata: {"finishReason":"ERROR","messageId":"m1"}\n\n',
    );
    reader.end();

    const h = handlers();
    await streamChat("b1", { question: "q", anchorChunk: 0, positionMs: null }, h, controller.signal);

    expect(h.onDelta).toHaveBeenCalledWith("partial");
    expect(h.onError).toHaveBeenCalledWith("LLM_UNAVAILABLE", "stopped early");
    expect(h.onDone).toHaveBeenCalledWith("ERROR", undefined);
  });

  it("a stream that ends with no `done` frame resolves and calls onDone with TRUNCATED", async () => {
    const controller = new AbortController();
    const { reader, response } = fakeStreamResponse(controller.signal);
    global.fetch = vi.fn().mockResolvedValue(response) as unknown as typeof fetch;

    reader.push(
      'event: meta\ndata: {"model":"m","enabled":true,"reason":null,"anchorChunk":0,"windowChunks":[0],"messageId":"m1"}\n\n' +
        'event: delta\ndata: {"text":"cut off"}\n\n',
    );
    reader.end(); // connection just... stops. No `done` frame.

    const h = handlers();
    await streamChat("b1", { question: "q", anchorChunk: 0, positionMs: null }, h, controller.signal);

    expect(h.onDone).toHaveBeenCalledWith("TRUNCATED");
  });

  it("an AbortSignal aborts the reader and does not call onDone", async () => {
    const controller = new AbortController();
    const { reader, response } = fakeStreamResponse(controller.signal);
    global.fetch = vi.fn().mockResolvedValue(response) as unknown as typeof fetch;

    reader.push(
      'event: meta\ndata: {"model":"m","enabled":true,"reason":null,"anchorChunk":0,"windowChunks":[0],"messageId":"m1"}\n\n',
    );

    const h = handlers();
    controller.abort();
    await streamChat("b1", { question: "q", anchorChunk: 0, positionMs: null }, h, controller.signal);

    expect(h.onDone).not.toHaveBeenCalled();
  });

  it("the request carries question/anchorChunk/positionMs and an Authorization: Bearer header", async () => {
    const controller = new AbortController();
    const { reader, response } = fakeStreamResponse(controller.signal);
    const fetchMock = vi.fn().mockResolvedValue(response);
    global.fetch = fetchMock as unknown as typeof fetch;
    reader.end();

    const h = handlers();
    await streamChat(
      "b1",
      { question: "What is this about?", anchorChunk: 7, positionMs: 12345 },
      h,
      controller.signal,
    );

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body as string)).toEqual({
      question: "What is this about?",
      anchorChunk: 7,
      positionMs: 12345,
    });
    expect((init.headers as Record<string, string>)["Authorization"]).toBe("Bearer id-token");
  });
});
