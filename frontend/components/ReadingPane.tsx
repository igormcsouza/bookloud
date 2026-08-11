"use client";

import { useEffect, useRef, useState } from "react";
import ChunkParagraph from "@/components/ChunkParagraph";
import type { Chunk } from "@/lib/books";

export type ReadingPaneProps = {
  chunks: Chunk[];
  /** The CHUNK index (`segments[segmentIndex].i`), not a position in
   *  `segments` -- the two diverge as soon as anything fails. */
  activeChunkIndex: number;
  wordStart: number;
  wordEnd: number;
  /** `manifest.missing`. */
  missingChunks: number[];
  estimatedTiming?: boolean;
  onSeekToChunk?: (chunkIndex: number) => void;
};

/** How long after a programmatic scroll a `scroll` event is still assumed to
 *  be ours rather than the user's. */
const PROGRAMMATIC_SCROLL_GRACE_MS = 600;

export default function ReadingPane({
  chunks,
  activeChunkIndex,
  wordStart,
  wordEnd,
  missingChunks,
  estimatedTiming = false,
  onSeekToChunk,
}: ReadingPaneProps) {
  const [followAlong, setFollowAlong] = useState(true);
  const lastAutoScrollAt = useRef(0);
  const missing = new Set(missingChunks);

  useEffect(() => {
    // A chunk change is what triggers the programmatic scroll, so record the
    // moment here and treat `scroll` events within the grace window as ours.
    lastAutoScrollAt.current = Date.now();
  }, [activeChunkIndex]);

  useEffect(() => {
    if (!followAlong) return;
    const onScroll = () => {
      // Scrolling back to re-read something must not fight the player, so a
      // manual scroll turns "follow along" off rather than being overridden
      // at the next chunk boundary.
      if (Date.now() - lastAutoScrollAt.current < PROGRAMMATIC_SCROLL_GRACE_MS) return;
      setFollowAlong(false);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [followAlong]);

  if (chunks.length === 0) {
    return (
      <div data-testid="reading-pane-skeleton" className="space-y-4" aria-hidden="true">
        {[0, 1, 2, 3].map((k) => (
          <div key={k} className="h-4 w-full animate-pulse rounded bg-ink-900" />
        ))}
      </div>
    );
  }

  return (
    <div data-testid="reading-pane">
      <label className="mb-4 flex items-center gap-2 text-xs text-sage">
        <input
          type="checkbox"
          data-testid="follow-along"
          checked={followAlong}
          onChange={(event) => setFollowAlong(event.target.checked)}
          className="accent-moss-400"
        />
        Follow along
      </label>

      <article className="prose-invert max-w-none text-lg">
        {chunks.map((chunk) => {
          const active = chunk.index === activeChunkIndex;
          return (
            <ChunkParagraph
              key={chunk.index}
              chunk={chunk}
              active={active}
              missing={missing.has(chunk.index)}
              // -1 for every inactive paragraph, so `React.memo` sees stable
              // props for all 329 of them while the word advances (Q11).
              wordStart={active ? wordStart : -1}
              wordEnd={active ? wordEnd : -1}
              estimatedTiming={active ? estimatedTiming : false}
              followAlong={followAlong}
              onSeekToChunk={onSeekToChunk}
            />
          );
        })}
      </article>
    </div>
  );
}
