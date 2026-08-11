"use client";

import React, { useEffect, useRef } from "react";
import type { Chunk } from "@/lib/books";

export type ChunkParagraphProps = {
  chunk: Chunk;
  active: boolean;
  /** `index ∈ manifest.missing` -- this chunk has no audio, so playback skips
   *  it. The text is still fully readable; that is the whole point (§9's rule
   *  1: if chunk text exists, it renders). */
  missing: boolean;
  /** CHUNK-relative character offsets; -1 when unknown or inactive. Chunk-
   *  relative is exactly what phase-4 §7.5's asymmetry buys: the slice below
   *  needs no global-offset bookkeeping at all. */
  wordStart: number;
  wordEnd: number;
  /** OQ-8: the segment's marks are interpolated, not measured. Rendered with
   *  a softer mark plus a tooltip -- the drift inside a long sentence is a
   *  real artefact, and pretending otherwise makes it look like a bug. */
  estimatedTiming?: boolean;
  /** The "follow along" toggle. Off means the player never moves the
   *  viewport, so scrolling back to re-read something doesn't fight it. */
  followAlong?: boolean;
  onSeekToChunk?: (chunkIndex: number) => void;
};

/**
 * One `<p>` per chunk, and **no per-word `<span>`s** (§6.4 Q10).
 *
 * Only the *active* chunk is sliced, into three text nodes and one `<mark>`.
 * A 330-chunk book would otherwise need ~100k word spans, which is not a
 * layout a browser should be asked to hold; this re-renders one paragraph at
 * word rate and leaves the other 329 untouched via `React.memo`.
 */
function ChunkParagraph({
  chunk,
  active,
  missing,
  wordStart,
  wordEnd,
  estimatedTiming = false,
  followAlong = true,
  onSeekToChunk,
}: ChunkParagraphProps) {
  const paragraphRef = useRef<HTMLParagraphElement | null>(null);
  const wasActive = useRef(false);

  useEffect(() => {
    // Auto-scroll on CHUNK change, not word change: moving the viewport every
    // ~400 ms would make the page unreadable. Anchored on the paragraph, not
    // the <mark>, because the <mark> does not exist yet during the one-round-
    // trip gap while the segment's marks load -- and that is exactly when the
    // user most needs to be shown where they are.
    if (followAlong && active && !wasActive.current) {
      paragraphRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    wasActive.current = active;
  }, [active, followAlong]);

  const highlighted = active && wordStart >= 0 && wordEnd > wordStart;

  return (
    <p
      ref={paragraphRef}
      data-testid={`chunk-${chunk.index}`}
      data-active={active ? "true" : undefined}
      data-missing={missing ? "true" : undefined}
      onDoubleClick={onSeekToChunk ? () => onSeekToChunk(chunk.index) : undefined}
      className={[
        "whitespace-pre-wrap leading-8 mb-6 rounded px-2 py-1 transition-colors",
        // When marks for the active segment aren't loaded yet the paragraph
        // still gets a tint, so the user always sees *where* they are during
        // the one-round-trip gap. "The highlight vanished" would otherwise
        // read as a bug (§6.4).
        active ? "bg-ink-900/70" : "",
        missing ? "text-sage" : "text-paper",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {missing ? (
        <span
          data-testid={`missing-marker-${chunk.index}`}
          className="mr-2 align-middle rounded bg-ink-800 px-1.5 py-0.5 text-xs uppercase tracking-wide text-sage"
          title="No audio for this section — playback skips it."
        >
          no audio
        </span>
      ) : null}
      {highlighted ? (
        <>
          {chunk.text.slice(0, wordStart)}
          <mark
            data-testid="active-word"
            data-timing={estimatedTiming ? "estimated" : "measured"}
            title={estimatedTiming ? "Approximate timing" : undefined}
            className={
              estimatedTiming
                ? "rounded bg-moss-400/40 text-paper"
                : "rounded bg-moss-400/80 text-ink-950"
            }
          >
            {chunk.text.slice(wordStart, wordEnd)}
          </mark>
          {chunk.text.slice(wordEnd)}
        </>
      ) : (
        chunk.text
      )}
    </p>
  );
}

/** Memoized on the props above, which is what keeps a word change from
 *  re-rendering all 330 paragraphs (Q11). `reading-pane.test.tsx` asserts
 *  this with a render-counting spy rather than trusting it. */
export default React.memo(ChunkParagraph);
