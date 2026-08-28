// Whether the reader may reveal (or safely determine there's nothing to
// apply for) the saved resume position -- issue #32. Revealing the reader
// before this is true is what causes the "flash at chunk 0, then teleport
// to the real position" bug: the screen must stay on its loading state
// until this is true, or until a bounded timeout gives up waiting (see
// `[bookId].tsx`).

import type { PlaybackError } from "@/hooks/usePlayback";
import type { ReadingProgress } from "@/lib/progress";

export type RestoreReadiness = {
  hasChunks: boolean;
  savedProgress: ReadingProgress | null | undefined;
  hasAudio: boolean;
  playbackReady: boolean;
  playbackError: PlaybackError | null;
};

export function canApplyRestore(r: RestoreReadiness): boolean {
  if (r.savedProgress === undefined) return false;
  if (!r.savedProgress) return true;
  if (!r.hasChunks) return false;
  if (!r.hasAudio) return true;
  return r.playbackReady || r.playbackError !== null;
}
