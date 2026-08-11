import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MEDIA_ERR_DECODE, usePlayback, type UsePlayback } from "@/hooks/usePlayback";
import type { BookManifest, Segment } from "@/lib/manifest";
import { NoAudioError } from "@/lib/books";

const getAudioUrlMock = vi.fn();
const getMarksDocumentMock = vi.fn();

vi.mock("@/lib/books", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/books")>();
  return {
    ...actual,
    getAudioUrl: (id: string) => getAudioUrlMock(id),
    getMarksDocument: (id: string, n: number) => getMarksDocumentMock(id, n),
  };
});

/** `MediaError.MEDIA_ERR_NETWORK`. jsdom defines no `MediaError` interface
 *  object, so the numeric spec values are used on both sides. */
const MEDIA_ERR_NETWORK = 2;

// --- a controllable media element + rAF -------------------------------------

/** jsdom's HTMLMediaElement is a stub: `play()` rejects, `currentTime` is
 *  read-only-ish and no events ever fire. This replaces exactly the surface
 *  the hook touches, so the test can drive `currentTime` frame by frame -- the
 *  only way to assert a 60 Hz loop's render count deterministically. */
function installMediaStub() {
  Object.defineProperty(HTMLMediaElement.prototype, "play", {
    configurable: true,
    value(this: HTMLMediaElement) {
      Object.defineProperty(this, "paused", { configurable: true, value: false });
      this.dispatchEvent(new Event("play"));
      return Promise.resolve();
    },
  });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", {
    configurable: true,
    value(this: HTMLMediaElement) {
      Object.defineProperty(this, "paused", { configurable: true, value: true });
      this.dispatchEvent(new Event("pause"));
    },
  });
  Object.defineProperty(HTMLMediaElement.prototype, "paused", {
    configurable: true,
    get: () => true,
  });
  let currentTime = 0;
  Object.defineProperty(HTMLMediaElement.prototype, "currentTime", {
    configurable: true,
    get: () => currentTime,
    set: (value: number) => {
      currentTime = value;
    },
  });
  Object.defineProperty(HTMLMediaElement.prototype, "duration", {
    configurable: true,
    get: () => 4,
  });
}

let frames: FrameRequestCallback[] = [];

function installRafStub() {
  frames = [];
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    frames.push(callback);
    return frames.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {
    frames = [];
  });
}

/** Run exactly one frame, the way the browser would: pop the queued callback
 *  and invoke it (which re-queues the next). */
async function tickFrame() {
  const next = frames.shift();
  if (!next) return;
  await act(async () => {
    next(performance.now());
  });
}

async function runFrames(count: number, onFrame?: (k: number) => void) {
  for (let k = 0; k < count; k += 1) {
    onFrame?.(k);
    await tickFrame();
  }
}

// --- fixtures ---------------------------------------------------------------

function segment(i: number, t: number, d: number, marksKey: string | null = "marks/k"): Segment {
  return { i, t, d, s: i * 100, e: (i + 1) * 100, audioKey: "audio/k", marksKey };
}

function manifestOf(segments: Segment[], audioKey: string | null = "audio/u/b/book.mp3"): BookManifest {
  return {
    version: 1,
    bookId: "book-1",
    status: audioKey ? "READY" : "PARTIAL",
    audioKey,
    durationMs: segments.reduce((sum, s) => sum + s.d, 0),
    chunksTotal: segments.length,
    chunksDone: segments.length,
    chunksFailed: 0,
    sampleRateHz: 24000,
    segments,
    missing: [],
  };
}

function marksDoc(words: { t: number; d: number; s: number; e: number; w: string }[], timing = "measured") {
  return { version: 1, bookId: "book-1", chunkIndex: 0, timing, words };
}

// A three-word chunk at 0/1000/2000 ms, and a second chunk starting at 3000.
const WORDS_A = [
  { t: 0, d: 400, s: 0, e: 3, w: "The" },
  { t: 1000, d: 400, s: 4, e: 7, w: "cat" },
  { t: 2000, d: 400, s: 8, e: 11, w: "sat" },
];
const WORDS_B = [
  { t: 0, d: 400, s: 0, e: 2, w: "On" },
  { t: 1000, d: 400, s: 3, e: 6, w: "mat" },
];

let renders = 0;
let api: UsePlayback | null = null;

function Harness({ manifest }: { manifest: BookManifest | null }) {
  renders += 1;
  const playback = usePlayback("book-1", manifest);
  api = playback;
  return (
    <audio ref={playback.audioRef} data-testid="audio" {...(playback.audioUrl ? { src: playback.audioUrl } : {})} />
  );
}

function setTime(element: HTMLMediaElement, ms: number) {
  element.currentTime = ms / 1000;
}

beforeEach(() => {
  installMediaStub();
  installRafStub();
  renders = 0;
  api = null;
  getAudioUrlMock.mockReset();
  getMarksDocumentMock.mockReset();
  getAudioUrlMock.mockResolvedValue({
    url: "https://s3/book.mp3?X-Amz-Signature=a",
    expiresIn: 3600,
    durationMs: 4000,
    contentType: "audio/mpeg",
  });
  getMarksDocumentMock.mockResolvedValue(marksDoc(WORDS_A));
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function mount(manifest: BookManifest | null) {
  const view = render(<Harness manifest={manifest} />);
  await waitFor(() => expect(api).not.toBeNull());
  const audio = view.container.querySelector("audio") as HTMLMediaElement;
  await act(async () => {});
  return { view, audio };
}

describe("usePlayback", () => {
  it("advances the word index as currentTime advances", async () => {
    const { audio } = await mount(manifestOf([segment(0, 0, 3000), segment(1, 3000, 2000)]));

    await act(async () => {
      await audio.play();
    });
    await waitFor(() => expect(getMarksDocumentMock).toHaveBeenCalled());

    setTime(audio, 100);
    await runFrames(2);
    expect(api!.state.wordStart).toBe(0);

    setTime(audio, 1100);
    await runFrames(2);
    expect(api!.state.wordStart).toBe(4);
    expect(api!.state.wordEnd).toBe(7);

    setTime(audio, 2100);
    await runFrames(2);
    expect(api!.state.wordStart).toBe(8);
  });

  it("re-renders only on an index change, not on every frame", async () => {
    // Q11: a 60 Hz setState would re-render the whole reading pane 60x/s. The
    // fast path (`stillInside`) is what keeps this to word rate.
    const { audio } = await mount(manifestOf([segment(0, 0, 3000)]));
    await act(async () => {
      await audio.play();
    });
    await waitFor(() => expect(getMarksDocumentMock).toHaveBeenCalled());

    setTime(audio, 0);
    await runFrames(2);
    const before = renders;

    // 60 frames spanning exactly two words (0 -> 1).
    await runFrames(60, (k) => setTime(audio, Math.floor((k / 60) * 1400)));

    expect(renders - before).toBeLessThanOrEqual(3);
  });

  it("crossing a segment boundary fetches the new segment and prefetches the next", async () => {
    getMarksDocumentMock.mockImplementation((_id: string, n: number) =>
      Promise.resolve(marksDoc(n === 0 ? WORDS_A : WORDS_B)),
    );
    const { audio } = await mount(
      manifestOf([segment(0, 0, 3000), segment(1, 3000, 2000), segment(2, 5000, 2000)]),
    );
    await act(async () => {
      await audio.play();
    });
    setTime(audio, 100);
    await runFrames(2);
    await waitFor(() => expect(getMarksDocumentMock).toHaveBeenCalledWith("book-1", 1));
    const afterFirst = getMarksDocumentMock.mock.calls.map((c) => c[1]).sort();
    expect(afterFirst).toEqual([0, 1]);

    setTime(audio, 3100);
    await runFrames(3);
    await waitFor(() =>
      expect(getMarksDocumentMock.mock.calls.map((c) => c[1]).sort()).toEqual([0, 1, 2]),
    );
    // Segment 1 was already prefetched, so crossing into it costs one request
    // (for segment 2), not two.
    expect(getMarksDocumentMock).toHaveBeenCalledTimes(3);
    expect(api!.state.chunkIndex).toBe(1);
  });

  it("a 404 marks response leaves wordStart -1 and makes ZERO further requests", async () => {
    // Without the negative cache this is a 60-requests-per-second loop from a
    // UI that merely looks stuck.
    getMarksDocumentMock.mockResolvedValue(null);
    const { audio } = await mount(manifestOf([segment(0, 0, 3000)]));
    await act(async () => {
      await audio.play();
    });
    setTime(audio, 100);
    await runFrames(2);
    await waitFor(() => expect(api!.state.marksPending).toBe(false));
    const after = getMarksDocumentMock.mock.calls.length;

    await runFrames(120, (k) => setTime(audio, k * 20));

    expect(api!.state.wordStart).toBe(-1);
    expect(api!.state.marksPending).toBe(false);
    expect(getMarksDocumentMock.mock.calls.length).toBe(after);
  });

  it("a segment with marksKey null never issues a request at all", async () => {
    const { audio } = await mount(manifestOf([segment(0, 0, 3000, null)]));
    await act(async () => {
      await audio.play();
    });
    setTime(audio, 100);
    await runFrames(4);

    expect(getMarksDocumentMock).not.toHaveBeenCalled();
    expect(api!.state.wordStart).toBe(-1);
  });

  it("an error event refreshes the URL once and restores currentTime", async () => {
    const { audio } = await mount(manifestOf([segment(0, 0, 3000)]));
    expect(getAudioUrlMock).toHaveBeenCalledTimes(1);

    setTime(audio, 1500);
    getAudioUrlMock.mockResolvedValue({
      url: "https://s3/book.mp3?X-Amz-Signature=fresh",
      expiresIn: 3600,
      durationMs: 4000,
      contentType: "audio/mpeg",
    });
    Object.defineProperty(audio, "error", {
      configurable: true,
      value: { code: MEDIA_ERR_NETWORK },
    });

    await act(async () => {
      audio.dispatchEvent(new Event("error"));
    });

    await waitFor(() => expect(getAudioUrlMock).toHaveBeenCalledTimes(2));
    expect(audio.currentTime).toBeCloseTo(1.5);
    expect(api!.state.error).toBeNull();
  });

  it("a fourth consecutive failure yields error URL_EXPIRED", async () => {
    const { audio } = await mount(manifestOf([segment(0, 0, 3000)]));
    Object.defineProperty(audio, "error", {
      configurable: true,
      value: { code: MEDIA_ERR_NETWORK },
    });

    for (let k = 0; k < 4; k += 1) {
      await act(async () => {
        audio.dispatchEvent(new Event("error"));
      });
      await waitFor(() => {});
    }

    expect(api!.state.error).toBe("URL_EXPIRED");
  });

  it("a decode failure is surfaced verbatim and never retried", async () => {
    // This is the signal that would catch a bad stitch; hiding it behind a
    // refresh loop would turn a data bug into a mystery.
    const { audio } = await mount(manifestOf([segment(0, 0, 3000)]));
    const before = getAudioUrlMock.mock.calls.length;
    Object.defineProperty(audio, "error", {
      configurable: true,
      value: { code: MEDIA_ERR_DECODE },
    });

    await act(async () => {
      audio.dispatchEvent(new Event("error"));
    });

    expect(api!.state.error).toBe("DECODE_FAILED");
    expect(getAudioUrlMock.mock.calls.length).toBe(before);
  });

  it("manifest.audioKey === null sets no src, never calls /audio, and play() is a no-op", async () => {
    const { audio } = await mount(manifestOf([], null));

    expect(api!.audioUrl).toBeNull();
    expect(audio.getAttribute("src")).toBeNull();
    expect(getAudioUrlMock).not.toHaveBeenCalled();
    expect(api!.state.error).toBe("NO_AUDIO");

    await act(async () => {
      api!.play();
    });
    expect(api!.state.playing).toBe(false);
  });

  it("a 409 from GET /audio agrees with the manifest and yields NO_AUDIO", async () => {
    getAudioUrlMock.mockRejectedValue(new NoAudioError());
    await mount(manifestOf([segment(0, 0, 3000)]));

    await waitFor(() => expect(api!.state.error).toBe("NO_AUDIO"));
    expect(api!.audioUrl).toBeNull();
  });

  it("visibilitychange to visible forces a full resync even when stillInside would pass", async () => {
    // rAF is throttled to ~0 Hz in a background tab, which is correct -- but a
    // paused tab has no frames at all, so the resnap must not wait for one.
    const { audio } = await mount(manifestOf([segment(0, 0, 3000)]));
    await act(async () => {
      await audio.play();
    });
    setTime(audio, 100);
    await runFrames(2);
    expect(api!.state.wordStart).toBe(0);

    // Jump forward with NO frames in between: the fast path's cached index
    // would still say "word 0".
    setTime(audio, 2100);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });

    expect(api!.state.wordStart).toBe(8);
  });

  it("durationMs comes from the manifest, not audio.duration", async () => {
    // audio.duration is stubbed at 4s; the manifest says 5s. For a
    // mixed-engine book the browser's figure is only an estimate.
    await mount(manifestOf([segment(0, 0, 3000), segment(1, 3000, 2000)]));
    expect(api!.state.durationMs).toBe(5000);
  });

  it("flags a >2% duration drift on loadedmetadata", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { audio } = await mount(manifestOf([segment(0, 0, 3000), segment(1, 3000, 2000)]));

    await act(async () => {
      audio.dispatchEvent(new Event("loadedmetadata"));
    });

    expect(api!.state.seekMayBeImprecise).toBe(true);
    expect(warn).toHaveBeenCalled();
  });

  it("seekMs sets currentTime and resnaps immediately", async () => {
    const { audio } = await mount(manifestOf([segment(0, 0, 3000)]));
    await act(async () => {
      await audio.play();
    });
    await waitFor(() => expect(getMarksDocumentMock).toHaveBeenCalled());

    await act(async () => {
      api!.seekMs(2100);
    });

    expect(audio.currentTime).toBeCloseTo(2.1);
    expect(api!.state.wordStart).toBe(8);
  });

  it("seekToChunk maps a CHUNK index to its segment start", async () => {
    // Chunk 4 is the second *segment* -- the two indexings diverge as soon as
    // anything fails, and confusing them would seek to the wrong place.
    const { audio } = await mount(manifestOf([segment(0, 0, 3000), segment(4, 3000, 2000)]));

    await act(async () => {
      api!.seekToChunk(4);
    });

    expect(audio.currentTime).toBeCloseTo(3);
  });

  it("seekToChunk for a chunk with no audio does nothing", async () => {
    const { audio } = await mount(manifestOf([segment(0, 0, 3000), segment(4, 3000, 2000)]));
    setTime(audio, 500);

    await act(async () => {
      api!.seekToChunk(2);
    });

    expect(audio.currentTime).toBeCloseTo(0.5);
  });

  it("the highlight is unaffected at 2x playback rate", async () => {
    // OQ-6: everything is derived from audio.currentTime, which is MEDIA
    // time, not wall-clock time -- so the rate cannot shift the mapping.
    const { audio } = await mount(manifestOf([segment(0, 0, 3000)]));
    await act(async () => {
      await audio.play();
    });
    await waitFor(() => expect(getMarksDocumentMock).toHaveBeenCalled());

    await act(async () => {
      api!.setPlaybackRate(2);
    });
    setTime(audio, 1100);
    await runFrames(2);

    expect(api!.playbackRate).toBe(2);
    expect(api!.state.wordStart).toBe(4);
    expect(api!.state.wordEnd).toBe(7);
  });

  it("loads marks when the manifest arrives AFTER mount", async () => {
    // The real mount sequence, and the one the other cases skip: the reader
    // mounts with `manifest === null` (it is fetched only once the book is
    // terminal). A MarksCache whose loader closed over `segments` would
    // capture the empty array permanently, find no segment for every
    // position, return null, and **negatively cache that forever** -- the
    // highlight would simply never appear, with no error anywhere.
    // Caught by e2e/playback.spec.ts before this test existed.
    const view = render(<Harness manifest={null} />);
    await waitFor(() => expect(api).not.toBeNull());
    await act(async () => {});
    expect(getMarksDocumentMock).not.toHaveBeenCalled();

    view.rerender(<Harness manifest={manifestOf([segment(0, 0, 3000)])} />);
    await act(async () => {});
    const audio = view.container.querySelector("audio") as HTMLMediaElement;
    await act(async () => {
      await audio.play();
    });
    setTime(audio, 1100);
    await runFrames(3);

    await waitFor(() => expect(getMarksDocumentMock).toHaveBeenCalledWith("book-1", 0));
    await waitFor(() => expect(api!.state.wordStart).toBe(4));
  });

  it("reports estimated timing so the mark can be rendered differently", async () => {
    getMarksDocumentMock.mockResolvedValue(marksDoc(WORDS_A, "estimated"));
    const { audio } = await mount(manifestOf([segment(0, 0, 3000)]));
    await act(async () => {
      await audio.play();
    });
    setTime(audio, 100);
    await runFrames(3);

    await waitFor(() => expect(api!.state.estimatedTiming).toBe(true));
  });
});
