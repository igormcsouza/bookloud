import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  BACKOFF_AFTER_MS,
  FAST_INTERVAL_MS,
  HARD_CAP_MS,
  SLOW_INTERVAL_MS,
  useBookStatus,
} from "@/hooks/useBookStatus";
import { BookNotFoundError, type BookStatusPayload } from "@/lib/books";

const getBookStatusMock = vi.fn();

vi.mock("@/lib/books", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/books")>();
  return { ...actual, getBookStatus: (id: string) => getBookStatusMock(id) };
});

function payload(overrides: Partial<BookStatusPayload> = {}): BookStatusPayload {
  return {
    id: "book-1",
    status: "EXTRACTED",
    terminal: false,
    progress: { chunksTotal: 3, chunksDone: 1, chunksFailed: 0, percent: 33 },
    failureReason: null,
    audio: { audioKey: null, manifestKey: null, durationMs: 0 },
    updatedAt: "2026-08-11T00:00:00Z",
    ...overrides,
  };
}

/** Advance fake timers AND let the pending promise microtasks settle -- the
 *  poll loop schedules its next tick only after the fetch resolves. */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  getBookStatusMock.mockReset();
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useBookStatus", () => {
  it("polls every 2s while terminal is false", async () => {
    getBookStatusMock.mockResolvedValue(payload());

    renderHook(() => useBookStatus("book-1"));
    await act(async () => {});
    expect(getBookStatusMock).toHaveBeenCalledTimes(1);

    await advance(FAST_INTERVAL_MS);
    expect(getBookStatusMock).toHaveBeenCalledTimes(2);

    await advance(FAST_INTERVAL_MS);
    expect(getBookStatusMock).toHaveBeenCalledTimes(3);
  });

  it("STOPS on the server's terminal flag", async () => {
    // Never a client-side status list: `status === "READY"` would never fire
    // for a PARTIAL book, i.e. for every book in local dev and every PR
    // environment (phase-5 §8.1).
    getBookStatusMock.mockResolvedValue(payload({ status: "PARTIAL", terminal: true }));

    const { result } = renderHook(() => useBookStatus("book-1"));
    await act(async () => {});

    await advance(FAST_INTERVAL_MS * 10);

    expect(getBookStatusMock).toHaveBeenCalledTimes(1);
    expect(result.current.status?.status).toBe("PARTIAL");
  });

  it("stops and reports notFound on a 404", async () => {
    getBookStatusMock.mockRejectedValue(new BookNotFoundError());

    const { result } = renderHook(() => useBookStatus("book-1"));
    await act(async () => {});

    await advance(FAST_INTERVAL_MS * 5);

    expect(result.current.notFound).toBe(true);
    expect(getBookStatusMock).toHaveBeenCalledTimes(1);
  });

  it("keeps polling (and keeps the last status) through a transient error", async () => {
    getBookStatusMock
      .mockResolvedValueOnce(payload())
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue(payload({ progress: { chunksTotal: 3, chunksDone: 2, chunksFailed: 0, percent: 67 } }));

    const { result } = renderHook(() => useBookStatus("book-1"));
    await act(async () => {});

    await advance(FAST_INTERVAL_MS);
    expect(result.current.status?.progress.chunksDone).toBe(1);
    expect(result.current.error).toBe("network");

    await advance(FAST_INTERVAL_MS);
    expect(result.current.status?.progress.chunksDone).toBe(2);
    expect(result.current.error).toBeNull();
  });

  it("backs off from 2s to 5s after 60s", async () => {
    getBookStatusMock.mockResolvedValue(payload());
    renderHook(() => useBookStatus("book-1"));
    await act(async () => {});

    await advance(BACKOFF_AFTER_MS + FAST_INTERVAL_MS);
    const before = getBookStatusMock.mock.calls.length;

    // A 2s step must no longer produce a poll...
    await advance(FAST_INTERVAL_MS);
    expect(getBookStatusMock.mock.calls.length).toBe(before);

    // ...but a 5s one must.
    await advance(SLOW_INTERVAL_MS - FAST_INTERVAL_MS);
    expect(getBookStatusMock.mock.calls.length).toBe(before + 1);
  });

  it("suspends while document.hidden and polls immediately on becoming visible", async () => {
    // Constraint 3: a hidden tab at 2s/poll is ~43,000 requests/day against
    // ~5 executions of API headroom.
    getBookStatusMock.mockResolvedValue(payload());
    renderHook(() => useBookStatus("book-1"));
    await act(async () => {});
    const initial = getBookStatusMock.mock.calls.length;

    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    await advance(FAST_INTERVAL_MS * 5);
    expect(getBookStatusMock.mock.calls.length).toBe(initial);

    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(getBookStatusMock.mock.calls.length).toBe(initial + 1);
  });

  it("enters stalled after the 15-minute hard cap", async () => {
    // A stranded book (phase-5 §4.3's named residual hole) would otherwise
    // poll from a tab left open overnight.
    getBookStatusMock.mockResolvedValue(payload());
    const { result } = renderHook(() => useBookStatus("book-1"));
    await act(async () => {});

    await advance(HARD_CAP_MS + SLOW_INTERVAL_MS);

    expect(result.current.stalled).toBe(true);
    const settled = getBookStatusMock.mock.calls.length;
    await advance(SLOW_INTERVAL_MS * 10);
    expect(getBookStatusMock.mock.calls.length).toBe(settled);
  });

  it("cleans its interval up on unmount", async () => {
    getBookStatusMock.mockResolvedValue(payload());
    const { unmount } = renderHook(() => useBookStatus("book-1"));
    await act(async () => {});
    const before = getBookStatusMock.mock.calls.length;

    unmount();
    await advance(FAST_INTERVAL_MS * 10);

    expect(getBookStatusMock.mock.calls.length).toBe(before);
  });

  it("adopt drops a payload into state and restarts a stopped loop", async () => {
    // The /resynthesize path: the book was terminal a moment ago, the 202
    // rewound it, and polling has to resume with no extra round trip.
    getBookStatusMock.mockResolvedValue(payload({ status: "PARTIAL", terminal: true }));
    const { result } = renderHook(() => useBookStatus("book-1"));
    await act(async () => {});
    expect(getBookStatusMock).toHaveBeenCalledTimes(1);

    getBookStatusMock.mockResolvedValue(payload({ status: "EXTRACTED", terminal: false }));
    await act(async () => {
      result.current.adopt(payload({ status: "EXTRACTED", terminal: false }));
    });

    expect(result.current.status?.status).toBe("EXTRACTED");
    await advance(FAST_INTERVAL_MS);
    expect(getBookStatusMock.mock.calls.length).toBeGreaterThan(1);
  });

  it("does not poll at all for a null bookId", async () => {
    renderHook(() => useBookStatus(null));
    await advance(FAST_INTERVAL_MS * 5);
    expect(getBookStatusMock).not.toHaveBeenCalled();
  });
});
