// Loads/saves the local "resume where I left off" position for a book
// (issue #22). Loading is one-shot per bookId; saving is throttled so a
// playing book isn't hitting AsyncStorage several times a second.

import { useCallback, useEffect, useRef, useState } from "react";
import { getProgress, saveProgress, type ReadingProgress } from "@/lib/progress";

/** How often to persist while actively playing/reading. Coarse on purpose --
 *  losing the last few seconds on a crash is an acceptable trade for not
 *  writing to disk every frame. */
const SAVE_THROTTLE_MS = 5_000;

export type UseReadingProgress = {
  /** The saved position for this book, or `null` once known there is none.
   *  `undefined` while still loading. */
  saved: ReadingProgress | null | undefined;
  /** Throttled persist -- safe to call on every position tick. */
  save: (chunkIndex: number, positionMs: number) => void;
  /** Bypasses the throttle -- use on unmount / navigating away. */
  saveNow: (chunkIndex: number, positionMs: number) => void;
};

export function useReadingProgress(bookId: string | undefined): UseReadingProgress {
  const [saved, setSaved] = useState<ReadingProgress | null | undefined>(undefined);
  const lastSaveRef = useRef(0);
  const pendingRef = useRef<{ chunkIndex: number; positionMs: number } | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setSaved(undefined);
    if (!bookId) return;
    let cancelled = false;
    void getProgress(bookId).then((progress) => {
      if (!cancelled) setSaved(progress);
    });
    return () => {
      cancelled = true;
    };
  }, [bookId]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [bookId]);

  const saveNow = useCallback(
    (chunkIndex: number, positionMs: number) => {
      if (!bookId) return;
      lastSaveRef.current = Date.now();
      pendingRef.current = null;
      void saveProgress(bookId, chunkIndex, positionMs);
    },
    [bookId],
  );

  const save = useCallback(
    (chunkIndex: number, positionMs: number) => {
      if (!bookId) return;
      pendingRef.current = { chunkIndex, positionMs };

      const elapsed = Date.now() - lastSaveRef.current;
      if (elapsed >= SAVE_THROTTLE_MS) {
        saveNow(chunkIndex, positionMs);
        return;
      }

      if (timerRef.current) return;
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        const pending = pendingRef.current;
        if (pending) saveNow(pending.chunkIndex, pending.positionMs);
      }, SAVE_THROTTLE_MS - elapsed);
    },
    [bookId, saveNow],
  );

  return { saved, save, saveNow };
}
