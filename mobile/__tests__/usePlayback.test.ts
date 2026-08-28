// Issue #32: (1) the audio-URL fetch must fire as soon as `bookId` is known,
// not gated on the `manifest` prop resolving first (the waterfall that made
// the reader slow to load); (2) a failing native load must retry with
// exponential backoff a bounded number of times, then give up with a
// visible error, rather than hanging or silently stranding the user.
//
// Issue #31 swapped the native engine to react-native-track-player (for its
// media-notification / lock-screen support); these tests moved with it, but
// the intent -- and, on purpose, the real-timers-throughout approach for
// exercising the backoff schedule -- is unchanged from before that swap.
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
const mockSetupPlayer = jest.fn().mockResolvedValue(undefined);
const mockUpdateOptions = jest.fn().mockResolvedValue(undefined);
const mockGetProgress = jest.fn().mockResolvedValue({ position: 0, duration: 60, buffered: 0 });
jest.mock("react-native-track-player", () => ({
  __esModule: true,
  default: {
    setupPlayer: (...args: unknown[]) => mockSetupPlayer(...args),
    updateOptions: (...args: unknown[]) => mockUpdateOptions(...args),
    load: (...args: unknown[]) => mockLoad(...args),
    reset: jest.fn().mockResolvedValue(undefined),
    play: jest.fn().mockResolvedValue(undefined),
    pause: jest.fn().mockResolvedValue(undefined),
    seekTo: jest.fn().mockResolvedValue(undefined),
    setRate: jest.fn().mockResolvedValue(undefined),
    getProgress: (...args: unknown[]) => mockGetProgress(...args),
    updateNowPlayingMetadata: jest.fn().mockResolvedValue(undefined),
    addEventListener: jest.fn().mockReturnValue({ remove: jest.fn() }),
  },
  Event: {
    PlaybackError: "playback-error",
    PlaybackState: "playback-state",
    PlaybackProgressUpdated: "playback-progress-updated",
  },
  State: { None: "none", Ready: "ready", Playing: "playing", Paused: "paused", Ended: "ended" },
  Capability: { Play: 0, Pause: 1, SeekTo: 2, Stop: 3 },
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
    mockSetupPlayer.mockClear();
    mockUpdateOptions.mockClear();
    mockGetProgress.mockClear();
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
      // A fresh URL each call, like a real presigned S3 URL would be --
      // returning the same string twice would make React bail out of
      // re-running the track-loading effect (no state change), masking the
      // retry entirely.
      let call = 0;
      mockGetAudioUrl.mockImplementation(() =>
        Promise.resolve({ ...AUDIO, url: `${AUDIO.url}?attempt=${call++}` }),
      );
      mockLoad.mockRejectedValue(new Error("native load failed"));

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
});
