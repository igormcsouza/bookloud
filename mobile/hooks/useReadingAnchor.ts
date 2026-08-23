// Simplified RN port of frontend/hooks/useReadingAnchor.ts. The web version
// falls back to an IntersectionObserver over the scrolled DOM when there is
// no active playback; RN has no DOM to observe. Since the reader shows one
// chunk/paragraph at a time (the design mock's "Now playing" screen, not a
// scrolling multi-chapter view), the anchor is simply: the playing chunk
// when audio is active, otherwise whatever chunk the reader is currently
// showing.

import { useEffect, useRef } from "react";

export function useReadingAnchor(
  playbackChunkIndex: number,
  visibleChunkIndex: number,
): React.MutableRefObject<number> {
  const anchorRef = useRef<number>(0);

  useEffect(() => {
    anchorRef.current = playbackChunkIndex >= 0 ? playbackChunkIndex : Math.max(0, visibleChunkIndex);
  }, [playbackChunkIndex, visibleChunkIndex]);

  return anchorRef;
}
