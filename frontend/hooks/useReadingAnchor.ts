import { useEffect, useRef } from "react";

export function useReadingAnchor(
  playbackChunkIndex: number,
  chunkCount: number
): React.MutableRefObject<number> {
  const anchorRef = useRef<number>(0);

  useEffect(() => {
    if (playbackChunkIndex >= 0) {
      anchorRef.current = Math.min(playbackChunkIndex, Math.max(0, chunkCount - 1));
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        let topmostIndex = -1;
        let topmostY = Infinity;

        for (const entry of entries) {
          if (entry.isIntersecting) {
            const index = Number(entry.target.getAttribute("data-chunk-index"));
            if (!isNaN(index) && entry.boundingClientRect.top < topmostY) {
              topmostY = entry.boundingClientRect.top;
              topmostIndex = index;
            }
          }
        }

        if (topmostIndex >= 0) {
          anchorRef.current = Math.min(topmostIndex, Math.max(0, chunkCount - 1));
        }
      },
      {
        root: null, // viewport
        rootMargin: "-10% 0px -70% 0px",
        threshold: 0,
      }
    );

    const elements = document.querySelectorAll("[data-chunk-index]");
    elements.forEach((el) => observer.observe(el));

    return () => observer.disconnect();
  }, [playbackChunkIndex, chunkCount]);

  return anchorRef;
}
