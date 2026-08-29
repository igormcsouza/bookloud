import { canApplyRestore, type RestoreReadiness } from "@/lib/readerReady";

function readiness(overrides: Partial<RestoreReadiness> = {}): RestoreReadiness {
  return {
    hasChunks: true,
    savedProgress: null,
    hasAudio: false,
    playbackReady: false,
    playbackError: null,
    ...overrides,
  };
}

describe("canApplyRestore", () => {
  it("is not ready while the saved progress hasn't loaded yet", () => {
    expect(canApplyRestore(readiness({ savedProgress: undefined }))).toBe(false);
  });

  it("is ready immediately when there's nothing saved, regardless of chunks/audio", () => {
    expect(
      canApplyRestore(readiness({ savedProgress: null, hasChunks: false, hasAudio: true })),
    ).toBe(true);
  });

  it("waits for the chunk list when there's a saved position to map it onto", () => {
    const saved = { chunkIndex: 3, positionMs: 1000, updatedAt: 0 };
    expect(canApplyRestore(readiness({ savedProgress: saved, hasChunks: false }))).toBe(false);
  });

  it("is ready once chunks are in, for a text-only (no audio) book", () => {
    const saved = { chunkIndex: 3, positionMs: 1000, updatedAt: 0 };
    expect(
      canApplyRestore(readiness({ savedProgress: saved, hasChunks: true, hasAudio: false })),
    ).toBe(true);
  });

  it("waits for the player when there's audio and it isn't ready or errored yet", () => {
    const saved = { chunkIndex: 3, positionMs: 1000, updatedAt: 0 };
    expect(
      canApplyRestore(
        readiness({ savedProgress: saved, hasAudio: true, playbackReady: false, playbackError: null }),
      ),
    ).toBe(false);
  });

  it("is ready once the player becomes ready", () => {
    const saved = { chunkIndex: 3, positionMs: 1000, updatedAt: 0 };
    expect(
      canApplyRestore(
        readiness({ savedProgress: saved, hasAudio: true, playbackReady: true, playbackError: null }),
      ),
    ).toBe(true);
  });

  it("gives up waiting on the player once it reports a terminal error", () => {
    const saved = { chunkIndex: 3, positionMs: 1000, updatedAt: 0 };
    expect(
      canApplyRestore(
        readiness({
          savedProgress: saved,
          hasAudio: true,
          playbackReady: false,
          playbackError: "URL_EXPIRED",
        }),
      ),
    ).toBe(true);
  });
});
