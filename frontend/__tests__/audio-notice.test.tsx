// One test per row of PLANS/phase-6.md §9's table, asserting the exact
// user-visible copy -- because §9 *is* constraint 2 ("show a simple message,
// but allow the user to continue reading"), and the copy is the deliverable.
//
// Row 6 (PARTIAL / NO_AUDIO / EXTERNAL_TTS_DISABLED) is the steady state of
// every environment CI can reach except local compose, so it is the row the
// PR pipeline's Playwright run actually exercises.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AudioNotice, { describeBook, modalChunkFailure } from "@/components/AudioNotice";
import type { BookStatus, BookStatusPayload, Chunk } from "@/lib/books";

function status(
  bookStatus: BookStatus,
  overrides: Partial<BookStatusPayload> = {},
): BookStatusPayload {
  const terminal = ["READY", "PARTIAL", "FAILED"].includes(bookStatus);
  return {
    id: "book-1",
    status: bookStatus,
    terminal,
    progress: { chunksTotal: 4, chunksDone: 2, chunksFailed: 0, percent: 50 },
    failureReason: null,
    audio: { audioKey: null, manifestKey: null, durationMs: 0 },
    updatedAt: "2026-08-11T00:00:00Z",
    ...overrides,
  };
}

function chunks(reason: string | null, count = 4): Chunk[] {
  return Array.from({ length: count }, (_, index) => ({
    index,
    text: `chunk ${index}`,
    charStart: index * 10,
    charEnd: (index + 1) * 10,
    audioKey: null,
    marksKey: null,
    status: reason ? ("FAILED" as const) : ("DONE" as const),
    pageStart: 1,
    pageEnd: 1,
    durationMs: 0,
    failureReason: reason,
    synthesisSource: null,
  }));
}

afterEach(cleanup);

describe("§9's degradation table, row by row", () => {
  it("row 1: UPLOADED/EXTRACTING -> skeleton, no player, 'Reading your PDF…'", () => {
    for (const value of ["UPLOADED", "EXTRACTING"] as BookStatus[]) {
      const { notice, player } = describeBook(status(value), []);
      expect(notice?.message).toBe("Reading your PDF…");
      expect(player).toBe("absent");
      expect(notice?.showRetry).toBe(false);
    }
  });

  it("row 2: EXTRACTED/STITCHING -> full text, disabled player, a percent bar", () => {
    // Chunks already exist here -- extraction writes them BEFORE the
    // EXTRACTED flip -- so this is a note beside the book, not a gate.
    for (const value of ["EXTRACTED", "STITCHING"] as BookStatus[]) {
      const { notice, player } = describeBook(status(value), []);
      expect(notice?.message).toBe("Preparing audio — 50%");
      expect(notice?.percent).toBe(50);
      expect(player).toBe("disabled");
    }
  });

  it("row 2b: an indeterminate bar while chunksTotal is still 0", () => {
    const { notice } = describeBook(
      status("EXTRACTED", { progress: { chunksTotal: 0, chunksDone: 0, chunksFailed: 0, percent: 0 } }),
      [],
    );
    expect(notice?.percent).toBeNull();
  });

  it("row 3: FAILED -> no player, the reason, and a Re-upload button", () => {
    const { notice, player } = describeBook(status("FAILED", { failureReason: "NO_TEXT_LAYER" }), []);
    expect(notice?.message).toBe("We couldn't read this PDF (NO_TEXT_LAYER).");
    expect(notice?.showReupload).toBe(true);
    expect(notice?.showRetry).toBe(false);
    expect(player).toBe("absent");
  });

  it("row 4: READY -> no notice at all, player enabled", () => {
    const { notice, player } = describeBook(status("READY"), []);
    expect(notice).toBeNull();
    expect(player).toBe("enabled");
  });

  it("row 5: PARTIAL with no failureReason -> player stays ENABLED, playback skips", () => {
    // Skipping a few paragraphs is far better than refusing to play.
    const { notice, player } = describeBook(
      status("PARTIAL", { progress: { chunksTotal: 4, chunksDone: 4, chunksFailed: 2, percent: 100 } }),
      [],
      { missingCount: 2 },
    );
    expect(notice?.message).toBe("2 of 4 sections have no audio — playback skips them.");
    expect(player).toBe("enabled");
    expect(notice?.showRetry).toBe(true);
  });

  it("row 6: PARTIAL/NO_AUDIO with EXTERNAL_TTS_DISABLED chunks", () => {
    const { notice, player } = describeBook(
      status("PARTIAL", { failureReason: "NO_AUDIO" }),
      chunks("EXTERNAL_TTS_DISABLED"),
    );
    expect(notice?.message).toBe(
      "Text-to-speech is turned off in this environment. You can still read the book.",
    );
    expect(player).toBe("absent");
    expect(notice?.showRetry).toBe(true);
  });

  it("row 7: PARTIAL/NO_AUDIO with ALL_ENGINES_FAILED chunks says something DIFFERENT", () => {
    const { notice, player } = describeBook(
      status("PARTIAL", { failureReason: "NO_AUDIO" }),
      chunks("ALL_ENGINES_FAILED"),
    );
    expect(notice?.message).toBe(
      "Every text-to-speech engine failed for this book. You can still read it.",
    );
    expect(player).toBe("absent");
  });

  it("row 8: PARTIAL/STITCH_FAILED -> disabled player (OQ-1 defers per-chunk playback)", () => {
    const { notice, player } = describeBook(status("PARTIAL", { failureReason: "STITCH_FAILED" }), []);
    expect(notice?.message).toBe("Audio couldn't be assembled.");
    expect(player).toBe("disabled");
    expect(notice?.showRetry).toBe(true);
  });

  it("row 9: manifest 404 -> no player, and the text still reads", () => {
    const { notice, player } = describeBook(status("READY"), [], { manifestMissing: true });
    expect(notice?.message).toBe("Audio information is unavailable. You can still read the book.");
    expect(player).toBe("absent");
  });

  it("'Try audio again' appears on exactly the PARTIAL rows (§9 rule 2)", () => {
    const nonPartial: BookStatus[] = ["UPLOADED", "EXTRACTING", "EXTRACTED", "STITCHING", "READY", "FAILED"];
    for (const value of nonPartial) {
      expect(describeBook(status(value), []).notice?.showRetry ?? false).toBe(false);
    }
    for (const reason of [null, "NO_AUDIO", "STITCH_FAILED"]) {
      const { notice } = describeBook(status("PARTIAL", { failureReason: reason }), []);
      expect(notice?.showRetry).toBe(true);
    }
  });

  it("no status yet -> no notice, no player", () => {
    expect(describeBook(null, [])).toEqual({ notice: null, player: "absent" });
  });
});

describe("modalChunkFailure", () => {
  it("picks the most common per-chunk reason", () => {
    const mixed = [...chunks("EXTERNAL_TTS_DISABLED", 3), ...chunks("ALL_ENGINES_FAILED", 1)];
    expect(modalChunkFailure(mixed)).toBe("EXTERNAL_TTS_DISABLED");
  });

  it("returns null when no chunk failed", () => {
    expect(modalChunkFailure(chunks(null))).toBeNull();
    expect(modalChunkFailure([])).toBeNull();
  });
});

describe("<AudioNotice>", () => {
  it("renders nothing for a null notice", () => {
    const { container } = render(<AudioNotice notice={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the message, the progress bar and the retry button", () => {
    const onRetryAudio = vi.fn();
    render(
      <AudioNotice
        notice={{ tone: "progress", message: "Preparing audio — 50%", percent: 50, showRetry: true, showReupload: false }}
        onRetryAudio={onRetryAudio}
      />,
    );

    expect(screen.getByTestId("audio-notice")).toHaveTextContent("Preparing audio — 50%");
    expect(screen.getByTestId("audio-progress")).toHaveAttribute("aria-valuenow", "50");
    screen.getByRole("button", { name: "Try audio again" }).click();
    expect(onRetryAudio).toHaveBeenCalledOnce();
  });

  it("disables the retry button while retrying", () => {
    render(
      <AudioNotice
        notice={{ tone: "warning", message: "x", percent: null, showRetry: true, showReupload: false }}
        onRetryAudio={vi.fn()}
        retrying
      />,
    );
    expect(screen.getByRole("button", { name: "Retrying…" })).toBeDisabled();
  });

  it("renders the re-upload button on the FAILED row", () => {
    const onReupload = vi.fn();
    render(
      <AudioNotice
        notice={{ tone: "error", message: "x", percent: null, showRetry: false, showReupload: true }}
        onReupload={onReupload}
      />,
    );
    screen.getByRole("button", { name: "Re-upload" }).click();
    expect(onReupload).toHaveBeenCalledOnce();
  });

  it("surfaces an action error alongside the notice", () => {
    render(
      <AudioNotice
        notice={{ tone: "warning", message: "x", percent: null, showRetry: false, showReupload: false }}
        error="Could not retry audio."
      />,
    );
    expect(screen.getByTestId("audio-notice")).toHaveTextContent("Could not retry audio.");
  });
});
