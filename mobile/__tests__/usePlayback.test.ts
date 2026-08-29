// Issue #32: (1) the audio-URL fetch must fire as soon as `bookId` is known,
// not gated on the `manifest` prop resolving first (the waterfall that made
// the reader slow to load); (2) a failing native load must retry with
// exponential backoff a bounded number of times, then give up with a
// visible error, rather than hanging or silently stranding the user.
//
// Issue #31 needed a lock-screen/notification transport, which led first to
// react-native-track-player and then -- once RNTP turned out to never
// deliver its playback events to JS under this app's React Native version
// (see usePlayback.ts's file header) -- to expo-audio, a first-party Expo
// module built for that runtime from the start. These tests moved with each
// swap, but the intent -- and, on purpose, the real-timers-throughout
// approach for exercising the backoff schedule -- is unchanged.
//
// Real timers throughout, on purpose: the backoff schedule (2s/4s/8s) is
// exercised at real wall-clock speed via `waitFor` rather than mocked with
// fake timers, which -- for a hook this deeply async (URL fetch -> native
// setup -> native track load, each its own awaited native call) -- proved
// to desync React's `act()` tracking across `it` blocks.

jest.mock("@react-native-async-storage/async-storage", () =>
  require("@react-native-async-storage/async-storage/jest/async-storage-mock"),
);

const mockLoad = jest.fn();
// Set by a test to make `createAudioPlayer` throw synchronously, standing in
// for a native init failure (see the retry test below). Reset in
// `beforeEach` so it never leaks between tests.
let mockPlayerLoadShouldThrow = false;
// Captures the `playbackStatusUpdate` callback usePlayback.ts registers, so
// a test can drive it directly -- native status updates never arrive on
// their own under Jest.
const mockStatusListeners: ((status: unknown) => void)[] = [];
const mockPlayer = {
  addListener: jest.fn((event: string, cb: (status: unknown) => void) => {
    if (event === "playbackStatusUpdate") mockStatusListeners.push(cb);
    return { remove: jest.fn() };
  }),
  replace: jest.fn(),
  play: jest.fn(),
  pause: jest.fn(),
  seekTo: jest.fn().mockResolvedValue(undefined),
  setPlaybackRate: jest.fn(),
  setActiveForLockScreen: jest.fn(),
  updateLockScreenMetadata: jest.fn(),
  remove: jest.fn(),
};
jest.mock("expo-audio", () => ({
  __esModule: true,
  createAudioPlayer: (...args: unknown[]) => {
    mockLoad(...args);
    // This expo-audio version's `AudioStatus` carries no error field (see
    // usePlayback.ts's comment where the status listener is registered) -- a
    // real decode/network failure is only ever caught by the load watchdog.
    // To keep the retry test's timing fast and deterministic without
    // waiting out that watchdog, `createAudioPlayer` itself throws
    // synchronously here when asked to -- a real failure mode too, and one
    // that exercises the exact same give-up/retry path.
    if (mockPlayerLoadShouldThrow) throw new Error("native load failed");
    return mockPlayer;
  },
  setAudioModeAsync: jest.fn().mockResolvedValue(undefined),
}));

const mockGetAudioUrl = jest.fn();
const mockGetMarksDocument = jest.fn();
jest.mock("@/lib/books", () => ({
  __esModule: true,
  // The real class, not a stand-in -- so `instanceof NoAudioError` inside
  // `usePlayback.ts` and the one this test throws are the same identity.
  NoAudioError: jest.requireActual("@/lib/books").NoAudioError,
  getAudioUrl: (...args: unknown[]) => mockGetAudioUrl(...args),
  getMarksDocument: (...args: unknown[]) => mockGetMarksDocument(...args),
}));

import { act, renderHook, waitFor } from "@testing-library/react-native";
import { NoAudioError } from "@/lib/books";
import type { BookManifest } from "@/lib/manifest";
import { MAX_URL_REFRESH_ATTEMPTS, usePlayback } from "@/hooks/usePlayback";

const AUDIO = {
  url: "https://example.com/book.mp3",
  expiresIn: 3600,
  durationMs: 60_000,
  contentType: "audio/mpeg",
};

describe("usePlayback", () => {
  beforeEach(() => {
    mockLoad.mockReset();
    mockGetAudioUrl.mockReset();
    mockGetMarksDocument.mockReset();
    mockPlayer.addListener.mockClear();
    mockPlayer.replace.mockClear();
    mockPlayerLoadShouldThrow = false;
    mockStatusListeners.length = 0;
  });

  it("fetches the audio URL as soon as bookId is known, without waiting on the manifest", async () => {
    mockGetAudioUrl.mockReturnValue(new Promise(() => {})); // never resolves in this test

    // `manifest` stays `null` throughout -- the old code derived "does this
    // book have audio" from `manifest?.audioKey` and never called
    // `getAudioUrl` until manifest resolved. It must fire regardless now.
    const { unmount } = await renderHook(() => usePlayback("book-1", null));
    await act(async () => {});

    expect(mockGetAudioUrl).toHaveBeenCalledWith("book-1");
    unmount();
  });

  it(
    "retries a failing native load with exponential backoff, then gives up",
    async () => {
      mockPlayerLoadShouldThrow = true;
      // A fresh URL each call, like a real presigned S3 URL would be --
      // returning the same string twice would make React bail out of
      // re-running the track-loading effect (no state change), masking the
      // retry entirely.
      let call = 0;
      mockGetAudioUrl.mockImplementation(() =>
        Promise.resolve({ ...AUDIO, url: `${AUDIO.url}?attempt=${call++}` }),
      );
      const { result, unmount } = await renderHook(() => usePlayback("book-1", null));

      await waitFor(() => expect(mockLoad).toHaveBeenCalledTimes(1));

      // Exhausts the 2s/4s/8s backoff schedule at real wall-clock speed.
      await waitFor(() => expect(result.current.state.error).toBe("URL_EXPIRED"), {
        timeout: 18_000,
      });
      expect(mockLoad).toHaveBeenCalledTimes(1 + MAX_URL_REFRESH_ATTEMPTS);

      unmount();
    },
    20_000,
  );

  it("does not treat a book with no audio as a failure needing retries", async () => {
    mockGetAudioUrl.mockRejectedValue(new NoAudioError());

    const { result, unmount } = await renderHook(() => usePlayback("book-1", null));

    await waitFor(() => expect(result.current.state.error).toBe("NO_AUDIO"));

    expect(mockLoad).not.toHaveBeenCalled();
    unmount();
  });

  // Regression: the highlight/auto-scroll would freeze mid-playback (while
  // still correcting itself once, briefly, whenever the app backgrounded and
  // came back) whenever the audio URL happened to resolve -- and so register
  // the native `playbackStatusUpdate` listener -- before the manifest did.
  // `usePlayback` fires the URL fetch as soon as `bookId` is known (issue
  // #32), independent of the manifest, so that race is the everyday case,
  // not an edge case. The listener is registered inside an effect keyed on
  // `[audioUrl]` alone and is never re-subscribed, so it must keep resyncing
  // against the CURRENT `segments` (populated once the manifest arrives)
  // rather than whatever `segments` looked like -- `[]` -- at registration
  // time.
  it("keeps resyncing against the current manifest after the audio URL resolves first", async () => {
    mockGetAudioUrl.mockResolvedValue(AUDIO);
    mockGetMarksDocument.mockResolvedValue({
      version: 1,
      bookId: "book-1",
      chunkIndex: 0,
      source: "edge-tts",
      voice: "test",
      timing: "measured",
      audioKey: "audio/000000.mp3",
      charStart: 0,
      charEnd: 11,
      durationMs: 5000,
      wordCount: 2,
      words: [
        { t: 0, d: 400, s: 0, e: 5, w: "Hello" },
        { t: 400, d: 400, s: 6, e: 11, w: "world" },
      ],
    });

    const { result, rerender, unmount } = await renderHook(
      ({ manifest }: { manifest: BookManifest | null }) => usePlayback("book-1", manifest),
      { initialProps: { manifest: null } },
    );

    // The URL resolves, and the player (with its status listener) gets set
    // up, while `manifest` -- and so `segments` -- is still empty.
    await waitFor(() => expect(mockStatusListeners.length).toBe(1));

    const manifest: BookManifest = {
      version: 1,
      bookId: "book-1",
      status: "READY",
      audioKey: "audio/book.mp3",
      durationMs: 5000,
      chunksTotal: 1,
      chunksDone: 1,
      chunksFailed: 0,
      sampleRateHz: null,
      segments: [
        { i: 0, t: 0, d: 5000, s: 0, e: 11, audioKey: "audio/000000.mp3", marksKey: "marks/000000.json" },
      ],
      missing: [],
    };
    // The manifest arrives after the listener already exists -- this is the
    // moment `sync`'s identity changes underneath it.
    rerender({ manifest });
    await act(async () => {});

    // A native progress tick while playing -- exactly what drives the
    // highlight during real playback, independent of the rAF loop (see
    // usePlayback.ts's "Defense in depth" comment on this same call).
    await act(async () => {
      mockStatusListeners[0]({ playing: true, currentTime: 0.2, isLoaded: true, duration: 5 });
    });
    await waitFor(() => expect(mockGetMarksDocument).toHaveBeenCalled());

    // The next tick, now that the marks fetch above has resolved, is what
    // should land the highlight -- proving the listener is resyncing against
    // the up-to-date `segments`, not the empty array it saw at registration.
    await act(async () => {
      mockStatusListeners[0]({ playing: true, currentTime: 0.2, isLoaded: true, duration: 5 });
    });

    await waitFor(() => expect(result.current.state.chunkIndex).toBe(0));
    expect(result.current.state.wordStart).toBe(0);
    expect(result.current.state.wordEnd).toBe(5);

    unmount();
  });
});
