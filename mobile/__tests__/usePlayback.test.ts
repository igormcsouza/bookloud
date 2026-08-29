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

// This expo-audio version's `AudioStatus` carries no error field (see
// usePlayback.ts's comment where the status listener is registered) -- a
// real decode/network failure is only ever caught by the load watchdog. To
// keep this test's timing fast and deterministic without waiting out that
// watchdog on every attempt, `createAudioPlayer` itself throws synchronously
// here, standing in for a native init failure -- a real failure mode too,
// and one that exercises the exact same give-up/retry path.
const mockLoad = jest.fn();
const mockPlayer = {
  addListener: jest.fn(() => ({ remove: jest.fn() })),
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
    throw new Error("native load failed");
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
